from django.conf import settings
from django.db import models


class Route(models.Model):
    """A planned journey between two districts, with a safety buffer."""

    ROUTING_SOURCES = [
        ("osrm", "Road route (OSRM)"),
        ("straight_line", "Straight-line fallback"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.CASCADE, related_name="routes",
        help_text="Owner. Only the owner can view or modify the route and its live trail.",
    )
    source = models.ForeignKey("core.District", on_delete=models.CASCADE, related_name="routes_from")
    destination = models.ForeignKey("core.District", on_delete=models.CASCADE, related_name="routes_to")
    waypoints = models.JSONField(help_text="List of [lat, lon] points describing the planned path")
    safety_buffer_km = models.FloatField(default=1.0, help_text="1.96 x sigma buffer radius around the route")
    buffer_sigma_km = models.FloatField(
        null=True, blank=True, help_text="Sigma behind the buffer: RMS spread of plausible paths (km)"
    )
    min_safety_score_crossed = models.FloatField(null=True, blank=True)
    route_safety_score = models.FloatField(
        null=True, blank=True, help_text="0-100 blend of mean and minimum district score along the path"
    )
    routing_source = models.CharField(max_length=20, choices=ROUTING_SOURCES, default="straight_line")
    distance_km = models.FloatField(null=True, blank=True)
    duration_min = models.FloatField(null=True, blank=True)
    alternatives = models.JSONField(
        default=list, blank=True,
        help_text="Every candidate path considered, with its safety metrics; the chosen one is flagged.",
    )
    original_waypoints = models.JSONField(
        null=True, blank=True, help_text="The first plan, kept after a reroute so the map can show both."
    )
    reroute_count = models.PositiveSmallIntegerField(default=0)
    contact_notified_at = models.DateTimeField(
        null=True, blank=True, help_text="When the owner's emergency contact was last emailed about this route."
    )
    created_at = models.DateTimeField(auto_now_add=True)
 
    def __str__(self):
        return f"Route {self.source.name} -> {self.destination.name} (#{self.pk})"
 
 
class GPSPing(models.Model):
    """A single location update along a route (Stage 5: live monitoring)."""
 
    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name="pings")
    sequence = models.PositiveIntegerField()
    latitude = models.FloatField()
    longitude = models.FloatField()
    nearest_district = models.ForeignKey(
        "core.District", null=True, blank=True, on_delete=models.SET_NULL
    )
    safety_score_at_point = models.FloatField(null=True, blank=True)
    in_unsafe_zone = models.BooleanField(default=False)
    timestamp = models.DateTimeField(auto_now_add=True)
 
    class Meta:
        ordering = ["route", "sequence"]
 
    def __str__(self):
        return f"Ping #{self.sequence} on route {self.route_id}"
 
 
class Alert(models.Model):
    """
    Raised when a GPS ping shows a deviation into an unsafe zone.
 
    The alert is shown to the user as an in-app popup with action choices
    (reroute / continue / dismiss / notify my emergency contact). SafeRoutes
    contacts no one else on its own: the user's emergency contact is emailed
    only if the user asks, or opted in to auto-escalation and left the alert
    unanswered for the on-screen countdown. Authorities are never contacted.
    See dashboard's alert modal (alert_modal.js).
    """
 
    SEVERITY_CHOICES = [("warning", "Warning"), ("critical", "Critical")]
    RESPONSE_CHOICES = [
        ("rerouted", "User chose: reroute me"),
        ("continued", "User chose: I'm okay, continue"),
        ("dismissed", "User dismissed the alert"),
        ("escalated", "User chose: notify my emergency contact"),
        ("auto_escalated", "No response: emergency contact notified"),
    ]
 
    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name="alerts")
    ping = models.ForeignKey(GPSPing, on_delete=models.CASCADE, related_name="alerts")
    severity = models.CharField(max_length=10, choices=SEVERITY_CHOICES, default="warning")
    message = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved = models.BooleanField(default=False)
    acknowledged = models.BooleanField(
        default=False, help_text="User has seen/actioned the popup for this alert."
    )
    user_response = models.CharField(
        max_length=20, choices=RESPONSE_CHOICES, blank=True,
        help_text="Which popup action the user chose, if any.",
    )
 
    class Meta:
        ordering = ["-created_at"]
 
    def __str__(self):
        return f"[{self.severity}] {self.message}"
 