"""
Tests for route geometry, safest-path selection, the CI buffer, live pings,
and access control on the route/alert API.

District figures come from core.testing fixtures, not real statistics. OSRM is
always mocked: no test touches the network.
"""
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from clustering.models import DistrictClusterAssignment
from core.models import District
from core.testing import make_clustered_districts
from tracking import services
from tracking.geometry import (
    buffer_polygon, distance_to_polyline_km, haversine_km, polyline_length_km,
    resample_polyline, sample_every_km, simplify_polyline,
)
from tracking.models import Alert, GPSPing, Route
from tracking.routing import Candidate, RoutingUnavailable

User = get_user_model()


class GeometryTests(SimpleTestCase):
    def test_point_on_a_long_segment_is_not_measured_to_the_nearest_vertex(self):
        # Thiruvananthapuram -> Kasaragod style: two vertices ~480 km apart.
        line = [(8.5, 77.0), (12.5, 77.0)]
        on_path = (10.5, 77.0)                                  # ~222 km from either vertex
        self.assertLess(distance_to_polyline_km(*on_path, line), 0.01)
        vertex_distance = min(haversine_km(*on_path, *p) for p in line)
        self.assertGreater(vertex_distance, 200)                # what the old vertex-only check reported

    def test_perpendicular_offset_is_measured_correctly(self):
        line = [(10.0, 76.0), (10.0, 77.0)]                     # east-west line
        d = distance_to_polyline_km(10.1, 76.5, line)           # 0.1 deg north of it
        self.assertAlmostEqual(d, 11.1, delta=0.2)

    def test_beyond_the_end_uses_the_end_vertex(self):
        line = [(10.0, 76.0), (10.0, 77.0)]
        self.assertAlmostEqual(distance_to_polyline_km(10.0, 77.1, line),
                               haversine_km(10.0, 77.1, 10.0, 77.0), delta=0.2)

    def test_single_point_and_empty_polylines(self):
        self.assertAlmostEqual(distance_to_polyline_km(10.0, 76.1, [(10.0, 76.0)]),
                               haversine_km(10.0, 76.1, 10.0, 76.0), places=6)
        with self.assertRaises(ValueError):
            distance_to_polyline_km(10.0, 76.0, [])

    def test_resample_is_evenly_spaced_and_keeps_the_ends(self):
        line = [(8.0, 76.0), (9.0, 76.0), (9.0, 77.0)]          # an L shape
        pts = resample_polyline(line, 9)
        self.assertEqual(len(pts), 9)
        self.assertEqual(pts[0], line[0])
        self.assertAlmostEqual(pts[-1][0], line[-1][0], places=6)
        self.assertAlmostEqual(pts[-1][1], line[-1][1], places=6)
        gaps = [haversine_km(*a, *b) for a, b in zip(pts, pts[1:])]
        self.assertLess(max(gaps) - min(gaps), max(gaps) * 0.2)  # roughly equal (corner cuts a little)

    def test_sample_every_km_respects_the_step(self):
        line = [(8.0, 76.0), (9.0, 76.0)]                       # ~111 km
        pts = sample_every_km(line, 5.0)
        self.assertGreaterEqual(len(pts), 22)
        self.assertAlmostEqual(polyline_length_km(pts), polyline_length_km(line), delta=0.5)

    def test_simplify_drops_collinear_points_but_keeps_ends(self):
        line = [(8.0 + i * 0.01, 76.0) for i in range(100)]
        out = simplify_polyline(line, 0.05)
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(out[0][0], 8.0, places=5)
        self.assertAlmostEqual(out[-1][0], 8.99, places=4)

    def test_buffer_polygon_encloses_points_within_radius_and_excludes_beyond(self):
        from shapely.geometry import Point, Polygon
        line = [(10.0, 76.0), (10.0, 76.5)]
        ring = buffer_polygon(line, 2.0)
        poly = Polygon([(lon, lat) for lat, lon in ring])
        near = Point(76.25, 10.0 + 1.0 / 111.0)                 # ~1 km north: inside
        far = Point(76.25, 10.0 + 4.0 / 111.0)                  # ~4 km north: outside
        self.assertTrue(poly.contains(near))
        self.assertFalse(poly.contains(far))

    def test_buffer_polygon_empty_for_zero_radius(self):
        self.assertEqual(buffer_polygon([(10.0, 76.0), (10.0, 76.5)], 0), [])


class _Base(TestCase):
    def setUp(self):
        make_clustered_districts()
        self.user = User.objects.create_user("rider@example.com", "rider@example.com", "pw-Fixture-123")
        self.other = User.objects.create_user("other@example.com", "other@example.com", "pw-Fixture-123")

    def district(self, name):
        return District.objects.get(name=name)

    def tier_of(self, name):
        return self.district(name).cluster_assignment.cluster.tier

    def score_of(self, name):
        return self.district(name).cluster_assignment.safety_score


def _line(a, b, n=40):
    return [(a[0] + (b[0] - a[0]) * i / (n - 1), a[1] + (b[1] - a[1]) * i / (n - 1)) for i in range(n)]


class PlanningTests(_Base):
    def _centre(self, name):
        d = self.district(name)
        return (d.latitude, d.longitude)

    def test_safest_candidate_wins_even_if_it_is_slower(self):
        src, dst = self.district("Kottayam"), self.district("Kozhikode")
        safest_name = max(District.objects.all(), key=lambda d: d.cluster_assignment.safety_score).name
        riskiest_name = min(District.objects.all(), key=lambda d: d.cluster_assignment.safety_score).name
        direct = _line(self._centre("Kottayam"), self._centre("Kozhikode"))
        via_safe = _line(self._centre("Kottayam"), self._centre(safest_name)) + \
            _line(self._centre(safest_name), self._centre("Kozhikode"))
        via_risky = _line(self._centre("Kottayam"), self._centre(riskiest_name)) + \
            _line(self._centre(riskiest_name), self._centre("Kozhikode"))
        # the engine ranks the "risky" one first and fastest
        cands = [Candidate(via_risky, 300, 240), Candidate(direct, 250, 300), Candidate(via_safe, 400, 420)]
        with mock.patch("tracking.services.fetch_candidates", return_value=cands):
            route = services.plan_route(self.user, src, dst)
        self.assertEqual(route.routing_source, "osrm")
        scores = {i: a["combined_score"] for i, a in enumerate(route.alternatives)}
        chosen = [a for a in route.alternatives if a["chosen"]]
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0]["combined_score"], max(scores.values()))
        self.assertEqual(route.route_safety_score, max(scores.values()))
        self.assertEqual(len(route.alternatives), 3)

    def test_routing_outage_falls_back_to_straight_line_and_says_so(self):
        with mock.patch("tracking.services.fetch_candidates", side_effect=RoutingUnavailable("down")):
            route = services.plan_route(self.user, self.district("Kollam"), self.district("Kannur"))
        self.assertEqual(route.routing_source, "straight_line")
        self.assertEqual(len(route.alternatives), 1)
        self.assertTrue(route.alternatives[0]["chosen"])
        self.assertGreater(route.distance_km, 300)

    def test_route_is_owned_by_the_planner(self):
        with mock.patch("tracking.services.fetch_candidates", side_effect=RoutingUnavailable("down")):
            route = services.plan_route(self.user, self.district("Kollam"), self.district("Kannur"))
        self.assertEqual(route.user, self.user)

    def test_buffer_is_1_96_sigma_and_is_clamped(self):
        chosen = _line((10.0, 76.0), (10.0, 77.0))
        parallel = [(lat + 1.0 / 111.0, lon) for lat, lon in chosen]  # runs 1 km north the whole way
        buffer_km, sigma = services.compute_buffer(chosen, [parallel])
        self.assertAlmostEqual(sigma, 1.0, delta=0.05)
        self.assertAlmostEqual(buffer_km, 1.96 * sigma, delta=0.05)

    def test_far_away_alternative_does_not_inflate_the_buffer(self):
        chosen = _line((10.0, 76.0), (10.0, 77.0))
        different_road = [(lat + 0.5, lon) for lat, lon in chosen]   # ~55 km away: not corridor spread
        buffer_km, sigma = services.compute_buffer(chosen, [different_road])
        self.assertEqual(sigma, services.STRAIGHT_LINE_SIGMA_KM)       # nothing usable -> fixed default
        self.assertEqual(buffer_km, round(1.96 * services.STRAIGHT_LINE_SIGMA_KM, 2))

    def test_buffer_never_exceeds_the_cap(self):
        chosen = _line((10.0, 76.0), (10.0, 77.0))
        wide = [(lat + 2.9 / 111.0, lon) for lat, lon in chosen]       # 2.9 km: inside corridor cap
        buffer_km, _sigma = services.compute_buffer(chosen, [wide])
        self.assertLessEqual(buffer_km, services.MAX_BUFFER_KM)

    def test_buffer_has_a_floor_for_identical_alternatives(self):
        chosen = _line((10.0, 76.0), (10.0, 77.0))
        _b, sigma = services.compute_buffer(chosen, [list(chosen)])
        self.assertEqual(sigma, services.MIN_SIGMA_KM)


class PingTests(_Base):
    def _route(self, path=None):
        a, b = self.district("Kollam"), self.district("Kannur")
        path = path or _line((a.latitude, a.longitude), (b.latitude, b.longitude), 12)
        return Route.objects.create(user=self.user, source=a, destination=b, waypoints=[list(p) for p in path],
                                    safety_buffer_km=1.0, routing_source="straight_line")

    def _unsafe_and_safe_district(self):
        unsafe = next((d for d in District.objects.all() if d.cluster_assignment.cluster.tier in services.UNSAFE_TIERS), None)
        self.assertIsNotNone(unsafe, "fixture should produce at least one Tier III/IV district")
        return unsafe

    def test_on_path_fix_raises_no_alert_even_in_an_unsafe_district(self):
        unsafe = self._unsafe_and_safe_district()
        route = self._route(path=_line((unsafe.latitude - 0.5, unsafe.longitude), (unsafe.latitude + 0.5, unsafe.longitude)))
        ping, alert = services.record_live_ping(route, unsafe.latitude, unsafe.longitude)
        self.assertIsNone(alert)
        self.assertFalse(ping.in_unsafe_zone)

    def test_deviation_into_an_unsafe_district_raises_one_alert(self):
        unsafe = self._unsafe_and_safe_district()
        route = self._route(path=_line((unsafe.latitude - 0.5, unsafe.longitude + 0.5),
                                       (unsafe.latitude + 0.5, unsafe.longitude + 0.5)))   # ~55 km east of the district
        ping, alert = services.record_live_ping(route, unsafe.latitude, unsafe.longitude)
        self.assertTrue(ping.in_unsafe_zone)
        self.assertIsNotNone(alert)
        self.assertIn(unsafe.name, alert.message)
        self.assertEqual(Alert.objects.filter(route=route).count(), 1)

    def test_deviation_into_a_safe_district_raises_no_alert(self):
        safest = max(District.objects.all(), key=lambda d: d.cluster_assignment.safety_score)
        self.assertEqual(safest.cluster_assignment.cluster.tier, "I")
        route = self._route(path=_line((safest.latitude - 0.5, safest.longitude + 0.5),
                                       (safest.latitude + 0.5, safest.longitude + 0.5)))
        ping, alert = services.record_live_ping(route, safest.latitude, safest.longitude)
        self.assertFalse(ping.in_unsafe_zone)
        self.assertIsNone(alert)

    def test_repeat_alerts_are_suppressed_until_the_first_is_acknowledged(self):
        unsafe = self._unsafe_and_safe_district()
        route = self._route(path=_line((unsafe.latitude - 0.5, unsafe.longitude + 0.5),
                                       (unsafe.latitude + 0.5, unsafe.longitude + 0.5)))
        services.record_live_ping(route, unsafe.latitude, unsafe.longitude)
        _p, second = services.record_live_ping(route, unsafe.latitude, unsafe.longitude)
        self.assertIsNone(second)
        route.alerts.update(acknowledged=True)
        _p, third = services.record_live_ping(route, unsafe.latitude, unsafe.longitude)
        self.assertIsNotNone(third)

    def test_gps_accuracy_radius_is_discounted_from_the_deviation(self):
        unsafe = self._unsafe_and_safe_district()
        # path passes 3 km east of the district centre: a fix with 5 km accuracy can't be called a deviation
        offset_lon = unsafe.longitude + 3.0 / (111.0 * 0.98)
        route = self._route(path=_line((unsafe.latitude - 0.5, offset_lon), (unsafe.latitude + 0.5, offset_lon)))
        route.safety_buffer_km = 1.0
        route.save()
        ping, alert = services.record_live_ping(route, unsafe.latitude, unsafe.longitude, accuracy_m=5000)
        self.assertFalse(ping.in_unsafe_zone)
        self.assertIsNone(alert)
        ping, _alert = services.record_live_ping(route, unsafe.latitude, unsafe.longitude, accuracy_m=10)
        self.assertTrue(ping.in_unsafe_zone)

    def test_ping_sequence_increments(self):
        route = self._route()
        a, _ = services.record_live_ping(route, 9.0, 76.5)
        b, _ = services.record_live_ping(route, 9.1, 76.5)
        self.assertEqual((a.sequence, b.sequence), (0, 1))

    def test_simulated_deviation_raises_an_alert_and_clean_run_raises_none(self):
        with mock.patch("tracking.services.fetch_candidates", side_effect=RoutingUnavailable("down")):
            route = services.plan_route(self.user, self.district("Kollam"), self.district("Kannur"))
        services.simulate_journey(route, inject_deviation=False)
        self.assertEqual(route.alerts.count(), 0)
        self.assertEqual(route.pings.count(), services.SIM_PING_COUNT)
        services.simulate_journey(route, inject_deviation=True)
        self.assertGreaterEqual(route.alerts.count(), 1)

    def test_reroute_keeps_history_and_closes_alerts(self):
        unsafe = self._unsafe_and_safe_district()
        route = self._route(path=_line((unsafe.latitude - 0.5, unsafe.longitude + 0.5),
                                       (unsafe.latitude + 0.5, unsafe.longitude + 0.5)))
        services.record_live_ping(route, unsafe.latitude, unsafe.longitude)
        original = route.waypoints
        with mock.patch("tracking.services.fetch_candidates", side_effect=RoutingUnavailable("down")):
            services.reroute_route(route)
        route.refresh_from_db()
        self.assertEqual(route.pings.count(), 1)                          # real trail preserved
        self.assertEqual(route.original_waypoints, original)
        self.assertEqual(route.reroute_count, 1)
        self.assertEqual(route.alerts.get().user_response, "rerouted")
        self.assertNotEqual(route.waypoints[0], original[0])              # re-planned from where they were
        self.assertAlmostEqual(route.waypoints[0][0], unsafe.latitude, places=3)


class ApiAccessTests(_Base):
    def setUp(self):
        super().setUp()
        a, b = self.district("Kollam"), self.district("Kannur")
        self.route = Route.objects.create(
            user=self.user, source=a, destination=b, safety_buffer_km=1.0, routing_source="straight_line",
            waypoints=[[a.latitude, a.longitude], [b.latitude, b.longitude]],
        )
        self.urls = {
            "data": reverse("dashboard:api_route_data", args=[self.route.pk]),
            "ping": reverse("dashboard:api_route_live_ping", args=[self.route.pk]),
            "ack": reverse("dashboard:api_route_acknowledge", args=[self.route.pk]),
            "reroute": reverse("dashboard:api_route_reroute", args=[self.route.pk]),
            "page": reverse("dashboard:route_detail", args=[self.route.pk]),
        }

    def _post(self, url, body):
        return self.client.post(url, json.dumps(body), content_type="application/json")

    def test_anonymous_api_calls_get_401_json_not_a_redirect(self):
        self.assertEqual(self.client.get(self.urls["data"]).status_code, 401)
        self.assertEqual(self._post(self.urls["ping"], {"lat": 9, "lon": 76}).status_code, 401)
        self.assertEqual(self._post(self.urls["ack"], {"choice": "dismissed"}).status_code, 401)
        self.assertEqual(self._post(self.urls["reroute"], {}).status_code, 401)
        self.assertEqual(GPSPing.objects.count(), 0)

    def test_anonymous_page_redirects_to_login(self):
        response = self.client.get(self.urls["page"])
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])
        self.assertEqual(self.client.get(reverse("dashboard:route_planner")).status_code, 302)
        self.assertEqual(self.client.get(reverse("dashboard:alerts")).status_code, 302)

    def test_another_users_route_looks_like_it_does_not_exist(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.urls["page"]).status_code, 404)
        self.assertEqual(self.client.get(self.urls["data"]).status_code, 404)
        self.assertEqual(self._post(self.urls["ping"], {"lat": 9, "lon": 76}).status_code, 404)
        self.assertEqual(self._post(self.urls["ack"], {"choice": "dismissed"}).status_code, 404)
        self.assertEqual(self._post(self.urls["reroute"], {}).status_code, 404)
        self.assertEqual(GPSPing.objects.count(), 0)

    def test_owner_can_read_route_and_gets_buffer_polygon(self):
        self.client.force_login(self.user)
        body = self.client.get(self.urls["data"]).json()
        self.assertEqual(body["source"], "Kollam")
        self.assertGreater(len(body["buffer_polygon"]), 4)
        self.assertEqual(body["routing_source"], "straight_line")

    def test_live_ping_validates_input(self):
        self.client.force_login(self.user)
        self.assertEqual(self._post(self.urls["ping"], {"lat": "x", "lon": 76}).status_code, 400)
        self.assertEqual(self._post(self.urls["ping"], {"lat": 95, "lon": 76}).status_code, 400)
        self.assertEqual(self._post(self.urls["ping"], {"lon": 76}).status_code, 400)
        ok = self._post(self.urls["ping"], {"lat": 9.5, "lon": 76.4, "accuracy": 12})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.route.pings.count(), 1)

    def test_acknowledge_rejects_unknown_choices(self):
        self.client.force_login(self.user)
        self.assertEqual(self._post(self.urls["ack"], {"choice": "call-police"}).status_code, 400)

    def test_get_is_not_allowed_on_mutating_endpoints(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.urls["ping"]).status_code, 405)
        self.assertEqual(self.client.get(self.urls["reroute"]).status_code, 405)

    def test_alert_list_only_shows_own_alerts(self):
        ping = GPSPing.objects.create(route=self.route, sequence=0, latitude=9, longitude=76)
        Alert.objects.create(route=self.route, ping=ping, message="OWN-ALERT-MARKER")
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get(reverse("dashboard:alerts")), "OWN-ALERT-MARKER")
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("dashboard:alerts")), "OWN-ALERT-MARKER")

    def test_planner_creates_a_route_owned_by_the_user(self):
        self.client.force_login(self.other)
        a, b = self.district("Kottayam"), self.district("Idukki")
        with mock.patch("tracking.services.fetch_candidates", side_effect=RoutingUnavailable("down")):
            response = self.client.post(reverse("dashboard:route_planner"),
                                        {"source": a.pk, "destination": b.pk})
        route = Route.objects.get(user=self.other)
        self.assertRedirects(response, reverse("dashboard:route_detail", args=[route.pk]))

    def test_planner_rejects_same_source_and_destination(self):
        self.client.force_login(self.user)
        d = self.district("Kottayam")
        response = self.client.post(reverse("dashboard:route_planner"), {"source": d.pk, "destination": d.pk})
        self.assertContains(response, "must be different")
        self.assertEqual(Route.objects.filter(user=self.user).count(), 1)  # only the setUp route

    def test_public_pages_render_with_and_without_data(self):
        for name in ("dashboard:home", "dashboard:heatmap", "dashboard:model_evaluation", "dashboard:api_clusters"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        DistrictClusterAssignment.objects.all().delete()
        District.objects.all().delete()
        for name in ("dashboard:home", "dashboard:heatmap", "dashboard:model_evaluation", "dashboard:api_clusters"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, f"{name} (empty database)")
