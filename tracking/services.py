"""
Stage 5: Maps & Cab Integration.

Route planning asks a road-routing engine (OSRM, see routing.py) for the
fastest route plus alternatives, scores every candidate against the safety
clusters, and picks the safest. If the engine can't be reached it falls back
to a straight line between the district centres and says so
(`Route.routing_source`).

Safety of a path = district safety scores sampled every SAMPLE_STEP_KM along
it (each sample takes the score of the nearest district centre -- the model's
granularity is the district, so that is as fine as the score can honestly be
resolved). A path's score blends its mean and its minimum so that a route
which is mostly safe but dips through one high-risk district is penalised.

The "off-route" check measures distance to the path *segments* (not just its
vertices), and the buffer is 1.96 x sigma, where sigma is estimated from how
far the alternative routes run from the chosen one (see `compute_buffer`).

`record_live_ping` ingests a real GPS fix; `simulate_journey` generates a
synthetic trip for demos.
"""
import math
from datetime import timedelta

from django.core.mail import send_mail
from django.db.models import Max
from django.utils import timezone

from clustering.models import DistrictClusterAssignment
from core.models import District

from .geometry import (
    distance_to_polyline_km, haversine_km, polyline_length_km, resample_polyline,
    sample_every_km, simplify_polyline,
)
from .models import Alert, GPSPing, Route
from .routing import RoutingUnavailable, fetch_candidates

UNSAFE_TIERS = {"III", "IV"}

SIM_PING_COUNT = 12
SAMPLE_STEP_KM = 5.0
MEAN_WEIGHT, MIN_WEIGHT = 0.7, 0.3          # path score = 0.7*mean + 0.3*min

CONFIDENCE_MULTIPLIER = 1.96                 # 95% two-sided
CORRIDOR_CAP_KM = 3.0                        # alternatives further than this are different roads, not corridor spread
MIN_SIGMA_KM = 0.25                          # floor: GPS noise + road snapping
STRAIGHT_LINE_SIGMA_KM = 0.5                 # no alternatives to estimate from
MAX_BUFFER_KM = 5.0

STORE_TOLERANCE_KM = 0.02                    # geometry simplification for the chosen path
ALT_STORE_TOLERANCE_KM = 0.1                 # ... and for the alternatives shown on the map
ALERT_DEDUPE_SECONDS = 120
CONTACT_COOLDOWN_MINUTES = 10                # at most one email to the emergency contact per route per window
EMERGENCY_NUMBER = "112"                     # India's single emergency number


class DistrictIndex:
    """District centres with their safety score/tier, loaded once per request."""

    def __init__(self):
        self.rows = []
        qs = District.objects.select_related("cluster_assignment__cluster")
        for d in qs:
            try:
                assignment = d.cluster_assignment
                score, tier = assignment.safety_score, assignment.cluster.tier
            except DistrictClusterAssignment.DoesNotExist:
                score, tier = None, None
            self.rows.append((d, score, tier))

    def nearest(self, lat, lon):
        """(district, score, tier, distance_km) of the closest district centre."""
        best = None
        for d, score, tier in self.rows:
            dist = haversine_km(lat, lon, d.latitude, d.longitude)
            if best is None or dist < best[3]:
                best = (d, score, tier, dist)
        return best

    def unsafe_districts(self):
        return [d for d, _s, tier in self.rows if tier in UNSAFE_TIERS]


# ---------------------------------------------------------------- scoring

def score_path(polyline, index):
    """Mean / min / blended safety score along a path, or None if nothing is scored."""
    scores = []
    for lat, lon in sample_every_km(polyline, SAMPLE_STEP_KM):
        _d, score, _tier, _dist = index.nearest(lat, lon)
        if score is not None:
            scores.append(score)
    if not scores:
        return None
    mean, low = sum(scores) / len(scores), min(scores)
    return {
        "mean": round(mean, 2),
        "min": round(low, 2),
        "combined": round(MEAN_WEIGHT * mean + MIN_WEIGHT * low, 2),
        "samples": len(scores),
    }


def compute_buffer(chosen, others):
    """
    1.96 x sigma corridor around the chosen path, in km.

    sigma is the RMS lateral offset of the alternative routes from the chosen
    one, counting only the stretches where an alternative runs within
    CORRIDOR_CAP_KM (parallel roads / carriageways -- places a legitimate
    traveller could plausibly be). Alternatives that are a different road
    entirely are excluded, otherwise one far-off option would inflate the
    buffer. With nothing to estimate from, a fixed sigma is used.
    Returns (buffer_km, sigma_km).
    """
    offsets = []
    for alt in others:
        for lat, lon in sample_every_km(alt, 2.0):
            d = distance_to_polyline_km(lat, lon, chosen)
            if d <= CORRIDOR_CAP_KM:
                offsets.append(d)
    if offsets:
        sigma = math.sqrt(sum(o * o for o in offsets) / len(offsets))
    else:
        sigma = STRAIGHT_LINE_SIGMA_KM
    sigma = max(sigma, MIN_SIGMA_KM)
    return round(min(CONFIDENCE_MULTIPLIER * sigma, MAX_BUFFER_KM), 2), round(sigma, 3)


# ---------------------------------------------------------------- planning

def _straight_line(origin, destination):
    return [
        (origin[0] + (destination[0] - origin[0]) * t / (SIM_PING_COUNT - 1),
         origin[1] + (destination[1] - origin[1]) * t / (SIM_PING_COUNT - 1))
        for t in range(SIM_PING_COUNT)
    ]


def _plan_paths(origin, destination, index):
    """Candidates -> scored -> safest chosen. Returns a dict of Route field values."""
    try:
        candidates = fetch_candidates(origin, destination)
        routing_source = "osrm"
        raw = [(c.polyline, c.distance_km, c.duration_min) for c in candidates]
    except RoutingUnavailable:
        line = _straight_line(origin, destination)
        routing_source = "straight_line"
        raw = [(line, polyline_length_km(line), None)]

    scored = []
    for polyline, dist_km, dur_min in raw:
        metrics = score_path(polyline, index)
        scored.append({"polyline": polyline, "distance_km": dist_km, "duration_min": dur_min, "metrics": metrics})

    def rank(item):
        m = item["metrics"]
        # safest first; ties go to the quicker / shorter path
        return (-(m["combined"] if m else -1), item["duration_min"] or item["distance_km"])

    scored.sort(key=rank)
    chosen = scored[0]
    others = [s["polyline"] for s in scored[1:]]
    buffer_km, sigma_km = compute_buffer(chosen["polyline"], others)

    alternatives = []
    for i, item in enumerate(scored):
        m = item["metrics"] or {}
        alternatives.append({
            "chosen": i == 0,
            "polyline": [[round(a, 5), round(b, 5)] for a, b in
                         simplify_polyline(item["polyline"], ALT_STORE_TOLERANCE_KM)],
            "distance_km": round(item["distance_km"], 1),
            "duration_min": round(item["duration_min"], 0) if item["duration_min"] else None,
            "mean_score": m.get("mean"),
            "min_score": m.get("min"),
            "combined_score": m.get("combined"),
        })

    stored = simplify_polyline(chosen["polyline"], STORE_TOLERANCE_KM)
    m = chosen["metrics"] or {}
    return {
        "waypoints": [[round(a, 6), round(b, 6)] for a, b in stored],
        "safety_buffer_km": buffer_km,
        "buffer_sigma_km": sigma_km,
        "min_safety_score_crossed": m.get("min"),
        "route_safety_score": m.get("combined"),
        "routing_source": routing_source,
        "distance_km": round(chosen["distance_km"], 1),
        "duration_min": round(chosen["duration_min"], 0) if chosen["duration_min"] else None,
        "alternatives": alternatives,
    }


def plan_route(user, source: District, destination: District) -> Route:
    index = DistrictIndex()
    fields = _plan_paths(
        (source.latitude, source.longitude), (destination.latitude, destination.longitude), index
    )
    return Route.objects.create(user=user, source=source, destination=destination, **fields)


def reroute_route(route: Route, lat=None, lon=None) -> Route:
    """
    Re-plan from where the traveller is now to the destination, picking the
    safest available path. Existing pings and alerts are kept (they are the
    record of what happened); open alerts are closed as 'rerouted'. The
    original plan is retained so the map can show both.
    """
    if lat is None or lon is None:
        trigger = (route.alerts.filter(acknowledged=False).select_related("ping").first())
        ping = trigger.ping if trigger else route.pings.order_by("-sequence").first()
        if ping:
            lat, lon = ping.latitude, ping.longitude
        else:
            lat, lon = route.source.latitude, route.source.longitude

    index = DistrictIndex()
    fields = _plan_paths((lat, lon), (route.destination.latitude, route.destination.longitude), index)

    if route.original_waypoints is None:
        route.original_waypoints = route.waypoints
    for name, value in fields.items():
        setattr(route, name, value)
    route.reroute_count += 1
    route.save()

    route.alerts.filter(acknowledged=False).update(
        acknowledged=True, user_response="rerouted", resolved=True
    )
    return route


# ---------------------------------------------------------------- pings

def _make_alert(route, ping, nearest, tier, score, deviation_km, live):
    prefix = "Live location deviation" if live else "Route deviation"
    return Alert.objects.create(
        route=route,
        ping=ping,
        severity="critical" if (score if score is not None else 100) < 35 else "warning",
        message=(
            f"{prefix} detected near {nearest.name}: {deviation_km:.1f} km off the planned "
            f"path, inside a Tier {tier} zone (safety score {score:.0f}/100)."
        ),
    )


def simulate_journey(route: Route, inject_deviation: bool = True):
    """
    Walk SIM_PING_COUNT points spread evenly along the planned path. If
    `inject_deviation`, one midpoint is pulled toward the nearest high-risk
    district centre (far enough to clear the buffer) to demo the alert system.
    Wipes the route's previous pings/alerts -- only call it on a fresh route.
    """
    route.pings.all().delete()
    route.alerts.all().delete()

    index = DistrictIndex()
    points = resample_polyline(route.waypoints, SIM_PING_COUNT)
    deviate_index = len(points) // 2 if inject_deviation else None

    pings, alerts = [], []
    for i, (lat, lon) in enumerate(points):
        actual_lat, actual_lon = lat, lon

        if i == deviate_index:
            unsafe = index.unsafe_districts()
            if unsafe:
                target = min(unsafe, key=lambda d: haversine_km(lat, lon, d.latitude, d.longitude))
                dist = haversine_km(lat, lon, target.latitude, target.longitude)
                if dist > 0:
                    pull = min(1.0, max(0.65, (2 * route.safety_buffer_km + 1.0) / dist))
                    actual_lat = lat + (target.latitude - lat) * pull
                    actual_lon = lon + (target.longitude - lon) * pull

        nearest, score, tier, _ = index.nearest(actual_lat, actual_lon)
        deviation_km = distance_to_polyline_km(actual_lat, actual_lon, route.waypoints)
        in_unsafe = bool(tier in UNSAFE_TIERS and deviation_km > route.safety_buffer_km)

        ping = GPSPing.objects.create(
            route=route, sequence=i,
            latitude=round(actual_lat, 6), longitude=round(actual_lon, 6),
            nearest_district=nearest, safety_score_at_point=score, in_unsafe_zone=in_unsafe,
        )
        pings.append(ping)
        if in_unsafe:
            alerts.append(_make_alert(route, ping, nearest, tier, score, deviation_km, live=False))

    return pings, alerts


def record_live_ping(route: Route, lat: float, lon: float, accuracy_m: float = None):
    """
    Ingest one real GPS fix. Deviation is the distance to the planned path
    minus the fix's own reported accuracy radius (so a noisy fix can't push
    someone over the buffer on its own). Appends to existing pings.

    A new alert is suppressed while an unacknowledged one was raised in the
    last ALERT_DEDUPE_SECONDS, so continuous tracking inside a risky area
    doesn't stack a popup every few seconds.
    """
    index = DistrictIndex()
    nearest, score, tier, _ = index.nearest(lat, lon)
    raw_deviation = distance_to_polyline_km(lat, lon, route.waypoints)
    accuracy_km = max(accuracy_m or 0.0, 0.0) / 1000.0
    deviation_km = max(raw_deviation - accuracy_km, 0.0)
    in_unsafe = bool(tier in UNSAFE_TIERS and deviation_km > route.safety_buffer_km)

    last = route.pings.aggregate(m=Max("sequence"))["m"]
    ping = GPSPing.objects.create(
        route=route, sequence=0 if last is None else last + 1,
        latitude=round(lat, 6), longitude=round(lon, 6),
        nearest_district=nearest, safety_score_at_point=score, in_unsafe_zone=in_unsafe,
    )

    alert = None
    if in_unsafe:
        recent = route.alerts.filter(
            acknowledged=False,
            created_at__gte=timezone.now() - timedelta(seconds=ALERT_DEDUPE_SECONDS),
        ).exists()
        if not recent:
            alert = _make_alert(route, ping, nearest, tier, score, raw_deviation, live=True)
    return ping, alert


# ---------------------------------------------------------------- emergency contact

class NoEmergencyContact(Exception):
    pass


class AutoEscalationNotAllowed(Exception):
    """Automatic escalation needs the user's opt-in AND an unanswered alert."""


class ContactThrottled(Exception):
    def __init__(self, retry_after_seconds):
        super().__init__(f"Try again in {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds


class ContactDeliveryError(Exception):
    pass


def _last_known_position(route):
    open_alert = route.alerts.filter(acknowledged=False).select_related("ping").first()
    ping = open_alert.ping if open_alert else route.pings.order_by("-sequence").first()
    return ping, open_alert


def notify_emergency_contact(route: Route, automatic: bool = False):
    """
    Email the route owner's emergency contact. Returns the contact.

    Manual (the user pressed the button) is always allowed. Automatic
    (the on-screen countdown ran out) additionally requires that the user
    opted in to auto-escalation and that an alert is still unanswered --
    the server checks this itself, so a forged request can't trigger it.
    At most one email per route per CONTACT_COOLDOWN_MINUTES.
    """
    from accounts.models import EmergencyContact  # local import: accounts is an optional layer

    user = route.user
    try:
        contact = user.emergency_contact
    except (AttributeError, EmergencyContact.DoesNotExist):
        raise NoEmergencyContact()

    ping, open_alert = _last_known_position(route)
    if automatic and not (contact.auto_escalate and open_alert):
        raise AutoEscalationNotAllowed()

    now = timezone.now()
    if route.contact_notified_at:
        wait = CONTACT_COOLDOWN_MINUTES * 60 - (now - route.contact_notified_at).total_seconds()
        if wait > 0:
            raise ContactThrottled(int(wait) + 1)

    who = user.email
    reason = (
        f"{who} did not respond to a SafeRoutes safety alert, so SafeRoutes is letting you know."
        if automatic else
        f"{who} asked SafeRoutes to let you know they may need help."
    )
    lines = [
        f"Hello {contact.name},", "", reason, "",
        f"Journey: {route.source.name} to {route.destination.name}",
        f"Time: {timezone.localtime(now):%d %b %Y, %H:%M %Z}",
    ]
    if open_alert:
        lines.append(f"Alert: {open_alert.message}")
    if ping:
        lines += [
            f"Last known position: {ping.latitude:.5f}, {ping.longitude:.5f}",
            f"Map: https://www.openstreetmap.org/?mlat={ping.latitude:.5f}&mlon={ping.longitude:.5f}"
            f"#map=15/{ping.latitude:.5f}/{ping.longitude:.5f}",
        ]
    else:
        lines.append("No live position has been recorded for this journey yet.")
    lines += [
        "", "This is an automated message and the position may be out of date. "
        f"If you are worried, try to reach {who} directly, or call the emergency number {EMERGENCY_NUMBER}.",
        "", "You are receiving this because they named you as their emergency contact on SafeRoutes.",
    ]
    try:
        send_mail(
            subject=f"SafeRoutes: {who} may need help",
            message="\n".join(lines), from_email=None, recipient_list=[contact.email],
        )
    except Exception as exc:
        raise ContactDeliveryError(str(exc)) from exc

    route.contact_notified_at = now
    route.save(update_fields=["contact_notified_at"])
    route.alerts.filter(acknowledged=False).update(
        acknowledged=True, user_response="auto_escalated" if automatic else "escalated", resolved=False,
    )
    return contact
