from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse

from .models import EmergencyContact

User = get_user_model()
URL = "accounts:emergency_contact"


class EmergencyContactPageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("rider@example.com", "rider@example.com", "pw-Fixture-123")
        self.client.force_login(self.user)

    def _save(self, **over):
        data = {"name": "Asha", "email": "asha@example.com", "confirm": "on"}
        data.update(over)
        return self.client.post(reverse(URL), data)

    def test_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse(URL))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])

    def test_saving_creates_the_contact_and_tells_them(self):
        self.assertRedirects(self._save(), reverse(URL))
        contact = EmergencyContact.objects.get(user=self.user)
        self.assertFalse(contact.auto_escalate)                 # off unless ticked
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["asha@example.com"])
        self.assertIn("rider@example.com", mail.outbox[0].subject)

    def test_confirmation_checkbox_is_required(self):
        response = self.client.post(reverse(URL), {"name": "Asha", "email": "asha@example.com"})
        self.assertContains(response, "confirm you")
        self.assertFalse(EmergencyContact.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_cannot_name_yourself(self):
        response = self._save(email="Rider@Example.com")
        self.assertContains(response, "other than yourself")
        self.assertFalse(EmergencyContact.objects.exists())

    def test_if_the_contact_cannot_be_told_nothing_is_saved(self):
        with mock.patch("accounts.views.send_mail", side_effect=OSError("smtp down")):
            response = self._save()
        self.assertContains(response, "nothing was saved")
        self.assertFalse(EmergencyContact.objects.exists())

    def test_changing_only_the_name_or_toggle_does_not_re_email_the_contact(self):
        self._save()
        mail.outbox.clear()
        self._save(name="Asha K", auto_escalate="on")
        contact = EmergencyContact.objects.get()
        self.assertEqual((contact.name, contact.auto_escalate), ("Asha K", True))
        self.assertEqual(len(mail.outbox), 0)

    def test_changing_the_address_tells_the_new_contact(self):
        self._save()
        mail.outbox.clear()
        self._save(email="bina@example.com")
        self.assertEqual(mail.outbox[0].to, ["bina@example.com"])
        self.assertEqual(EmergencyContact.objects.count(), 1)

    def test_remove_deletes_the_contact(self):
        self._save()
        self.client.post(reverse(URL), {"remove": "1"})
        self.assertFalse(EmergencyContact.objects.exists())

    def test_users_cannot_see_each_others_contact(self):
        self._save()
        other = User.objects.create_user("o@example.com", "o@example.com", "pw-Fixture-123")
        self.client.force_login(other)
        self.assertNotContains(self.client.get(reverse(URL)), "asha@example.com")
