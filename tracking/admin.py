from django.contrib import admin
from .models import Route, GPSPing, Alert


class GPSPingInline(admin.TabularInline):
    model = GPSPing
    extra = 0
    readonly_fields = ("sequence", "latitude", "longitude", "nearest_district", "safety_score_at_point", "in_unsafe_zone", "timestamp")
    can_delete = False


@admin.register(Route)
class RouteAdmin(admin.ModelAdmin):
    list_display = ("__str__", "user", "routing_source", "route_safety_score", "safety_buffer_km", "reroute_count", "contact_notified_at", "created_at")
    list_filter = ("routing_source",)
    search_fields = ("user__email",)
    inlines = [GPSPingInline]


@admin.register(GPSPing)
class GPSPingAdmin(admin.ModelAdmin):
    list_display = ("route", "sequence", "latitude", "longitude", "in_unsafe_zone", "safety_score_at_point", "timestamp")
    list_filter = ("in_unsafe_zone",)


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ("route", "severity", "message", "user_response", "resolved", "created_at")
    list_filter = ("severity", "resolved", "user_response")
