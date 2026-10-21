import json
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from clustering.models import ClusterEvaluation, DistrictClusterAssignment, ModelRun, SafetyCluster
from core.models import District
from tracking.geometry import buffer_polygon
from tracking.models import Alert, Route
from tracking import services as tracking_services
from tracking.services import plan_route, record_live_ping, reroute_route, simulate_journey
from django.contrib import messages


def api_login_required(view):
    """Like login_required, but answers JSON 401 instead of redirecting to the login page."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "authentication required"}, status=401)
        return view(request, *args, **kwargs)
    return wrapper


def _own_route_or_404(request, route_id):
    """Routes are private: someone else's route is indistinguishable from a missing one."""
    return get_object_or_404(Route, pk=route_id, user=request.user)


def _contact_payload(user):
    """What the browser may know about the emergency contact: never the address."""
    contact = getattr(user, "emergency_contact", None) if user.is_authenticated else None
    return {"name": contact.name, "auto_escalate": contact.auto_escalate} if contact else None


def _alert_payload(alert):
    return {
        "id": alert.id,
        "sequence": alert.ping.sequence,
        "severity": alert.severity,
        "message": alert.message,
        "acknowledged": alert.acknowledged,
    }


def home(request):
    clusters = SafetyCluster.objects.filter(algorithm="kmeans").order_by("-avg_safety_score")
    districts = District.objects.select_related("cluster_assignment__cluster").order_by("name")
    recent_alerts = (
        Alert.objects.filter(route__user=request.user).select_related("route", "ping")[:5]
        if request.user.is_authenticated else []
    )
    return render(request, "dashboard/home.html", {
        "clusters": clusters,
        "districts": districts,
        "recent_alerts": recent_alerts,
        "has_data": districts.exists(),
        "has_clusters": clusters.exists(),
    })


def heatmap(request):
    return render(request, "dashboard/heatmap.html", {
        "has_clusters": SafetyCluster.objects.filter(algorithm="kmeans").exists(),
    })


def model_evaluation(request):
    run = ModelRun.objects.order_by("-trained_at").first()
    context = {"run": run}
    if run:
        evals = list(run.evaluations.order_by("algorithm", "k"))
        context.update({
            "evaluations": evals,
            "kmeans_evals": [e for e in evals if e.algorithm == "kmeans"],
            "k_min": min((e.k for e in evals), default=None),
            "k_max": max((e.k for e in evals), default=None),
            "gmm_evals": [e for e in evals if e.algorithm == "gmm"],
            "chart_data": {
                "k": [e.k for e in evals if e.algorithm == "kmeans"],
                "inertia": [e.inertia for e in evals if e.algorithm == "kmeans"],
                "kmeans_silhouette": [e.silhouette for e in evals if e.algorithm == "kmeans"],
                "gmm_silhouette": [e.silhouette for e in evals if e.algorithm == "gmm"],
                "gmm_bic": [e.bic for e in evals if e.algorithm == "gmm"],
            },
            "pearson": _matrix_rows(run.pearson),
            "spearman": _matrix_rows(run.spearman),
            "clusters": SafetyCluster.objects.filter(algorithm="kmeans").order_by("-avg_safety_score"),
        })
    return render(request, "dashboard/model_evaluation.html", context)


def _matrix_rows(matrix):
    if not matrix:
        return None
    cols = list(matrix.keys())
    return {"cols": cols, "rows": [(r, [matrix[c][r] for c in cols]) for r in cols]}


@login_required
def route_planner(request):
    districts = District.objects.order_by("name")
    if request.method == "POST":
        destination = get_object_or_404(District, pk=request.POST.get("destination"))

        if request.POST.get("use_my_location") == "1":
            try:
                lat, lon = float(request.POST["origin_lat"]), float(request.POST["origin_lon"])
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError
            except (KeyError, ValueError):
                return render(request, "dashboard/route_planner.html", {
                    "districts": districts,
                    "error": "Couldn't read your location. Allow location access and try again."})
            _d, _s, _t, dist_km = tracking_services.DistrictIndex().nearest(lat, lon)
            if dist_km > 150:
                return render(request, "dashboard/route_planner.html", {
                    "districts": districts,
                    "error": "Your location is outside Kerala, which is all SafeRoutes covers."})
            route = plan_route(request.user, None, destination, origin=(lat, lon))
            return redirect(reverse("dashboard:route_detail", args=[route.pk]))

        source = get_object_or_404(District, pk=request.POST.get("source"))
        # ... rest unchanged

    wants_sim = request.POST.get("simulate_deviation") == "on"
    pings, alerts = simulate_journey(route, inject_deviation=wants_sim)
    if wants_sim and not alerts:
        messages.info(request, "No high-risk district lies off this route, so there was no deviation to simulate.")
        

    return render(request, "dashboard/route_planner.html", {"districts": districts})


@login_required
def route_detail(request, route_id):
    route = _own_route_or_404(request, route_id)
    return render(request, "dashboard/route_detail.html", {
        "route": route,
        "alerts": route.alerts.all(),
        "alternatives": route.alternatives,
        "emergency_contact": _contact_payload(request.user),
    })


@login_required
def alerts_list(request):
    alerts = Alert.objects.filter(route__user=request.user).select_related(
        "route", "route__source", "route__destination", "ping"
    )
    return render(request, "dashboard/alerts.html", {"alerts": alerts})


# ---------- JSON endpoints consumed by the map JS ----------

def api_clusters(request):
    data = []
    for assignment in DistrictClusterAssignment.objects.select_related("district", "cluster"):
        d = assignment.district
        data.append({
            "id": d.id,
            "name": d.name,
            "lat": d.latitude,
            "lon": d.longitude,
            "safety_score": assignment.safety_score,
            "tier": assignment.cluster.tier,
            "tier_label": assignment.cluster.get_tier_display(),
            "color": assignment.cluster.color_hex,
            "crime_rate_per_100k": d.crime_rate_per_100k,
            "police_per_100k": d.police_per_100k,
            "literacy_rate": d.literacy_rate,
        })
    return JsonResponse({"districts": data})


@api_login_required
def api_route_data(request, route_id):
    route = _own_route_or_404(request, route_id)
    pings = [{
        "sequence": p.sequence,
        "lat": p.latitude,
        "lon": p.longitude,
        "in_unsafe_zone": p.in_unsafe_zone,
        "safety_score": p.safety_score_at_point,
        "nearest_district": p.nearest_district.name if p.nearest_district else None,
    } for p in route.pings.select_related("nearest_district")]
    alerts = [_alert_payload(a) for a in route.alerts.select_related("ping")]
    return JsonResponse({
        "source": route.source_label,
        "destination": route.destination.name,
        "waypoints": route.waypoints,
        "original_waypoints": route.original_waypoints,
        "alternatives": [a for a in route.alternatives if not a.get("chosen")],
        "buffer_polygon": buffer_polygon(route.waypoints, route.safety_buffer_km),
        "safety_buffer_km": route.safety_buffer_km,
        "routing_source": route.routing_source,
        "min_safety_score_crossed": route.min_safety_score_crossed,
        "route_safety_score": route.route_safety_score,
        "pings": pings,
        "alerts": alerts,
        "emergency_contact": _contact_payload(request.user),
        "destination_lat": route.destination.latitude,
        "destination_lon": route.destination.longitude,
    })


def _json_body(request):
    try:
        return json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return {}


@require_POST
@api_login_required
def api_route_reroute(request, route_id):
    """
    "Reroute me" popup action. Re-plans from the traveller's current position
    (the position of the alert that triggered the popup, unless the client
    supplies lat/lon) to the destination along the safest available path.
    Recorded pings and alerts are kept; open alerts are closed as 'rerouted'.
    """
    route = _own_route_or_404(request, route_id)
    payload = _json_body(request)
    try:
        lat = float(payload["lat"]) if "lat" in payload else None
        lon = float(payload["lon"]) if "lon" in payload else None
    except (TypeError, ValueError):
        return JsonResponse({"error": "lat/lon must be numbers"}, status=400)

    route = reroute_route(route, lat, lon)
    return JsonResponse({
        "status": "rerouted",
        "routing_source": route.routing_source,
        "route_safety_score": route.route_safety_score,
    })


@require_POST
@api_login_required
def api_route_acknowledge(request, route_id):
    """
    "I'm okay, continue" / "Dismiss" popup actions. Marks the route's
    unresolved alerts as acknowledged, recording which choice was made.
    Does NOT notify anyone else -- this only updates alert state.
    """
    route = _own_route_or_404(request, route_id)
    choice = _json_body(request).get("choice")
    if choice not in {"continued", "dismissed"}:
        return JsonResponse({"error": "invalid choice"}, status=400)

    updated = route.alerts.filter(acknowledged=False).update(
        acknowledged=True,
        user_response=choice,
        resolved=(choice == "continued"),
    )
    return JsonResponse({"status": "ok", "updated": updated})


@require_POST
@api_login_required
def api_route_live_ping(request, route_id):
    """
    Ingests one real GPS fix from the user's browser (navigator.geolocation)
    for a route that belongs to them, evaluates it against the safety clusters
    and planned path, and returns any alert raised. The fix is stored as a
    GPSPing on this route, visible only to the route's owner and site admins
    -- SafeRoutes does not share live location with any third party.
    """
    route = _own_route_or_404(request, route_id)
    payload = _json_body(request)
    try:
        lat = float(payload["lat"])
        lon = float(payload["lon"])
        accuracy = float(payload["accuracy"]) if payload.get("accuracy") is not None else None
    except (KeyError, TypeError, ValueError):
        return JsonResponse({"error": "lat and lon are required"}, status=400)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return JsonResponse({"error": "lat/lon out of range"}, status=400)

    ping, alert = record_live_ping(route, lat, lon, accuracy_m=accuracy)
    return JsonResponse({
        "status": "ok",
        "ping": {
            "sequence": ping.sequence,
            "lat": ping.latitude,
            "lon": ping.longitude,
            "in_unsafe_zone": ping.in_unsafe_zone,
            "safety_score": ping.safety_score_at_point,
            "nearest_district": ping.nearest_district.name if ping.nearest_district else None,
        },
        "alert": _alert_payload(alert) if alert else None,
    })


@require_POST
@api_login_required
def api_route_notify_contact(request, route_id):
    """
    Email the user's emergency contact. `{"automatic": true}` is only honoured
    when the user opted in to auto-escalation and an alert is unanswered.
    """
    route = _own_route_or_404(request, route_id)
    automatic = bool(_json_body(request).get("automatic"))
    try:
        contact = tracking_services.notify_emergency_contact(route, automatic=automatic)
    except tracking_services.NoEmergencyContact:
        return JsonResponse({"error": "You haven't set an emergency contact yet."}, status=409)
    except tracking_services.AutoEscalationNotAllowed:
        return JsonResponse({"error": "Automatic notification isn't enabled or there is no open alert."}, status=403)
    except tracking_services.ContactThrottled as exc:
        return JsonResponse(
            {"error": f"Your contact was just notified. You can send another in {exc.retry_after_seconds}s.",
             "retry_after": exc.retry_after_seconds}, status=429)
    except tracking_services.ContactDeliveryError:
        return JsonResponse({"error": "We couldn't send the email. Please try again, or call the emergency number."}, status=502)
    return JsonResponse({"status": "notified", "contact": contact.name, "automatic": automatic})
