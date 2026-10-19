"""
Emergency-contact escalation: the rules that stop it emailing someone it shouldn't.
Fixture data only; no network.
"""
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import EmergencyContact
from core.models import District
from core.testing import make_clustered_districts
from tracking import services
from tracking.models import Alert, GPSPing, Route

User = get_user_model()


class EscalationTests(TestCase):
    def setUp(self):
        make_clustered_districts()
        self.user = User.objects.create_user("rider@example.com", "rider@example.com", "pw-Fixture-123")
        self.other = User.objects.create_user("other@example.com", "other@example.com", "pw-Fixture-123")
        a, b = District.objects.get(name="Kollam"), District.objects.get(name="Kannur")
        self.route = Route.objects.create(
            user=self.user, source=a, destination=b, safety_buffer_km=1.0, routing_source="straight_line",
            waypoints=[[a.latitude, a.longitude], [b.latitude, b.longitude]],
        )
        self.url = reverse("dashboard:api_route_notify_contact", args=[self.route.pk])
        self.client.force_login(self.user)

    def _contact(self, auto=False):
        return EmergencyContact.objects.create(user=self.user, name="Asha", email="asha@example.com", auto_escalate=auto)

    def _open_alert(self):
        ping = GPSPing.objects.create(route=self.route, sequence=0, latitude=9.5012, longitude=76.3401)
        return Alert.objects.create(route=self.route, ping=ping, message="Deviation near Alappuzha")

    def _post(self, body=None):
        return self.client.post(self.url, json.dumps(body or {}), content_type="application/json")

    def test_no_contact_set_sends_nothing(self):
        response = self._post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(mail.outbox), 0)

    def test_manual_notify_emails_only_the_contact_with_journey_and_position(self):
        self._contact()
        self._open_alert()
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["asha@example.com"])
        self.assertIn("Kollam to Kannur", msg.body)
        self.assertIn("9.50120, 76.34010", msg.body)
        self.assertIn("openstreetmap.org/?mlat=9.50120&mlon=76.34010", msg.body)
        self.assertIn("112", msg.body)
        self.assertIn("asked SafeRoutes", msg.body)
        self.route.refresh_from_db()
        self.assertIsNotNone(self.route.contact_notified_at)
        self.assertEqual(Alert.objects.get().user_response, "escalated")

    def test_manual_notify_works_without_an_alert_or_position(self):
        self._contact()
        self.assertEqual(self._post().status_code, 200)
        self.assertIn("No live position", mail.outbox[0].body)

    def test_cooldown_blocks_a_second_email(self):
        self._contact()
        self.assertEqual(self._post().status_code, 200)
        second = self._post()
        self.assertEqual(second.status_code, 429)
        self.assertEqual(len(mail.outbox), 1)
        self.route.contact_notified_at = timezone.now() - timedelta(minutes=services.CONTACT_COOLDOWN_MINUTES + 1)
        self.route.save()
        self.assertEqual(self._post().status_code, 200)
        self.assertEqual(len(mail.outbox), 2)

    def test_automatic_is_refused_unless_the_user_opted_in(self):
        self._contact(auto=False)
        self._open_alert()
        self.assertEqual(self._post({"automatic": True}).status_code, 403)
        self.assertEqual(len(mail.outbox), 0)

    def test_automatic_is_refused_without_an_unanswered_alert(self):
        self._contact(auto=True)
        self.assertEqual(self._post({"automatic": True}).status_code, 403)       # no alert at all
        Alert.objects.all().delete()
        alert = self._open_alert()
        alert.acknowledged = True
        alert.save()
        self.assertEqual(self._post({"automatic": True}).status_code, 403)       # alert already answered
        self.assertEqual(len(mail.outbox), 0)

    def test_automatic_with_opt_in_and_open_alert_sends_and_is_recorded(self):
        self._contact(auto=True)
        self._open_alert()
        self.assertEqual(self._post({"automatic": True}).status_code, 200)
        self.assertIn("did not respond", mail.outbox[0].body)
        self.assertEqual(Alert.objects.get().user_response, "auto_escalated")

    def test_delivery_failure_reports_502_and_changes_nothing(self):
        self._contact()
        self._open_alert()
        with mock.patch("tracking.services.send_mail", side_effect=OSError("smtp down")):
            response = self._post()
        self.assertEqual(response.status_code, 502)
        self.route.refresh_from_db()
        self.assertIsNone(self.route.contact_notified_at)          # so the user can retry at once
        self.assertFalse(Alert.objects.get().acknowledged)

    def test_another_users_route_cannot_trigger_a_notification(self):
        self._contact()
        self.client.force_login(self.other)
        self.assertEqual(self._post().status_code, 404)
        self.assertEqual(len(mail.outbox), 0)

    def test_anonymous_cannot_trigger_and_get_is_not_allowed(self):
        self._contact()
        self.client.logout()
        self.assertEqual(self._post().status_code, 401)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertEqual(len(mail.outbox), 0)

    def test_contact_address_is_never_sent_to_the_browser(self):
        self._contact(auto=True)
        data = self.client.get(reverse("dashboard:api_route_data", args=[self.route.pk])).json()
        self.assertEqual(data["emergency_contact"], {"name": "Asha", "auto_escalate": True})
        self.assertNotIn("asha@example.com", json.dumps(data))
        page = self.client.get(reverse("dashboard:route_detail", args=[self.route.pk]))
        self.assertNotContains(page, "asha@example.com")

    def test_route_data_without_a_contact_has_none(self):
        data = self.client.get(reverse("dashboard:api_route_data", args=[self.route.pk])).json()
        self.assertIsNone(data["emergency_contact"])
