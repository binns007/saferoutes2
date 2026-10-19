from django.urls import path
from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.home, name="home"),
    path("heatmap/", views.heatmap, name="heatmap"),
    path("routes/plan/", views.route_planner, name="route_planner"),
    path("routes/<int:route_id>/", views.route_detail, name="route_detail"),
    path("alerts/", views.alerts_list, name="alerts"),
    path("model/", views.model_evaluation, name="model_evaluation"),

    path("api/clusters/", views.api_clusters, name="api_clusters"),
    path("api/routes/<int:route_id>/", views.api_route_data, name="api_route_data"),
    path("api/routes/<int:route_id>/reroute/", views.api_route_reroute, name="api_route_reroute"),
    path("api/routes/<int:route_id>/acknowledge/", views.api_route_acknowledge, name="api_route_acknowledge"),
    path("api/routes/<int:route_id>/live-ping/", views.api_route_live_ping, name="api_route_live_ping"),
    path("api/routes/<int:route_id>/notify-contact/", views.api_route_notify_contact, name="api_route_notify_contact"),
]
