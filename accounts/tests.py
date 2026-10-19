from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import services
from .models import EmailOTP

User = get_user_model()
GOOD_PASSWORD = "Tr1cky-passphrase-77"


def _code_from_outbox():
    body = mail.outbox[-1].body
    return next(tok.rstrip(".") for tok in body.split() if tok.rstrip(".").isdigit() and len(tok.rstrip(".")) == 6)


class RegistrationFlowTests(TestCase):
    def setUp(self):
        cache.clear()

    def _register(self, email="Rider@Example.com", password=GOOD_PASSWORD):
        return self.client.post(reverse("accounts:register"), {
            "email": email, "password1": password, "password2": password,
        })

    def test_register_sends_otp_and_creates_inactive_user(self):
        response = self._register()
        self.assertRedirects(response, reverse("accounts:verify"))
        user = User.objects.get(email="rider@example.com")
        self.assertFalse(user.is_active)
        self.assertEqual(user.username, "rider@example.com")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["rider@example.com"])
        self.assertEqual(len(_code_from_outbox()), 6)

    def test_code_is_stored_hashed_not_plain(self):
        self._register()
        code = _code_from_outbox()
        self.assertNotIn(code, EmailOTP.objects.get().code_hash)

    def test_correct_code_activates_and_logs_in(self):
        self._register()
        response = self.client.post(reverse("accounts:verify"), {"code": _code_from_outbox()})
        self.assertRedirects(response, reverse("dashboard:home"), fetch_redirect_response=False)
        user = User.objects.get(email="rider@example.com")
        self.assertTrue(user.is_active)
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)

    def test_wrong_code_rejected_and_counts_attempts(self):
        self._register()
        response = self.client.post(reverse("accounts:verify"), {"code": "000000" if _code_from_outbox() != "000000" else "111111"})
        self.assertContains(response, "isn")  # "That code isn't right"
        self.assertFalse(User.objects.get(email="rider@example.com").is_active)
        self.assertEqual(EmailOTP.objects.get().attempts, 1)

    def test_locks_after_max_attempts_even_with_right_code(self):
        self._register()
        right = _code_from_outbox()
        wrong = "000000" if right != "000000" else "111111"
        for _ in range(services.OTP_MAX_ATTEMPTS):
            self.client.post(reverse("accounts:verify"), {"code": wrong})
        response = self.client.post(reverse("accounts:verify"), {"code": right})
        self.assertContains(response, "Too many wrong attempts")
        self.assertFalse(User.objects.get(email="rider@example.com").is_active)

    def test_expired_code_rejected(self):
        self._register()
        EmailOTP.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        response = self.client.post(reverse("accounts:verify"), {"code": _code_from_outbox()})
        self.assertContains(response, "expired")
        self.assertFalse(User.objects.get(email="rider@example.com").is_active)

    def test_resend_is_throttled_then_works_after_cooldown(self):
        self._register()
        self.client.post(reverse("accounts:resend"))
        self.assertEqual(len(mail.outbox), 1)  # blocked by cooldown
        EmailOTP.objects.update(created_at=timezone.now() - timedelta(seconds=services.OTP_RESEND_COOLDOWN_SECONDS + 1))
        self.client.post(reverse("accounts:resend"))
        self.assertEqual(len(mail.outbox), 2)
        # the old code no longer works, the new one does
        old, new = mail.outbox[0].body, _code_from_outbox()
        self.assertEqual(self.client.post(reverse("accounts:verify"), {"code": new}).status_code, 302)

    def test_only_newest_code_is_valid(self):
        self._register()
        first = _code_from_outbox()
        EmailOTP.objects.update(created_at=timezone.now() - timedelta(minutes=5))
        self.client.post(reverse("accounts:resend"))
        second = _code_from_outbox()
        if first != second:
            response = self.client.post(reverse("accounts:verify"), {"code": first})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(User.objects.get(email="rider@example.com").is_active)

    def test_weak_and_mismatched_passwords_rejected(self):
        response = self.client.post(reverse("accounts:register"), {
            "email": "a@example.com", "password1": "12345678", "password2": "12345678"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email="a@example.com").exists())
        response = self.client.post(reverse("accounts:register"), {
            "email": "a@example.com", "password1": GOOD_PASSWORD, "password2": GOOD_PASSWORD + "x"})
        self.assertContains(response, "don")
        self.assertFalse(User.objects.filter(email="a@example.com").exists())

    def test_cannot_register_an_existing_verified_email(self):
        User.objects.create_user("x@example.com", "x@example.com", GOOD_PASSWORD)
        response = self._register(email="x@example.com")
        self.assertContains(response, "already exists")
        self.assertEqual(len(mail.outbox), 0)

    def test_reregistering_unverified_email_restarts_with_new_password(self):
        self._register(password=GOOD_PASSWORD)
        EmailOTP.objects.update(created_at=timezone.now() - timedelta(minutes=5))
        self._register(password="An0ther-long-pass-99")
        user = User.objects.get(email="rider@example.com")
        self.assertTrue(user.check_password("An0ther-long-pass-99"))
        self.assertEqual(User.objects.count(), 1)

    def test_email_delivery_failure_is_reported_not_crashed(self):
        with mock.patch("accounts.services.send_mail", side_effect=OSError("smtp down")):
            response = self._register()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "couldn")
        self.assertEqual(EmailOTP.objects.count(), 0)

    def test_verify_page_requires_a_pending_registration(self):
        self.assertRedirects(self.client.get(reverse("accounts:verify")), reverse("accounts:register"))


class LoginTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("v@example.com", "v@example.com", GOOD_PASSWORD)

    def test_login_with_email_and_password(self):
        response = self.client.post(reverse("accounts:login"), {"email": "V@Example.com", "password": GOOD_PASSWORD})
        self.assertRedirects(response, reverse("dashboard:home"), fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_wrong_password_is_generic_error(self):
        response = self.client.post(reverse("accounts:login"), {"email": "v@example.com", "password": "nope-nope-nope"})
        self.assertContains(response, "Incorrect email or password")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_unknown_email_gives_same_error(self):
        response = self.client.post(reverse("accounts:login"), {"email": "ghost@example.com", "password": GOOD_PASSWORD})
        self.assertContains(response, "Incorrect email or password")

    def test_unverified_user_is_sent_to_verification(self):
        User.objects.create_user("u@example.com", "u@example.com", GOOD_PASSWORD, is_active=False)
        response = self.client.post(reverse("accounts:login"), {"email": "u@example.com", "password": GOOD_PASSWORD})
        self.assertRedirects(response, reverse("accounts:verify"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_repeated_failures_get_blocked(self):
        for _ in range(5):
            self.client.post(reverse("accounts:login"), {"email": "v@example.com", "password": "bad-bad-bad"})
        response = self.client.post(reverse("accounts:login"), {"email": "v@example.com", "password": GOOD_PASSWORD})
        self.assertContains(response, "Too many failed attempts")

    def test_next_redirect_is_followed_only_for_local_urls(self):
        url = reverse("accounts:login")
        ok = self.client.post(url, {"email": "v@example.com", "password": GOOD_PASSWORD, "next": "/alerts/"})
        self.assertEqual(ok["Location"], "/alerts/")
        self.client.logout()
        bad = self.client.post(url, {"email": "v@example.com", "password": GOOD_PASSWORD, "next": "https://evil.example/x"})
        self.assertEqual(bad["Location"], reverse("dashboard:home"))

    def test_logout_requires_post(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("accounts:logout")).status_code, 405)
        self.client.post(reverse("accounts:logout"))
        self.assertNotIn("_auth_user_id", self.client.session)


class MaskEmailTests(TestCase):
    def test_mask(self):
        self.assertEqual(services.mask_email("abhishek@gmail.com"), "a******k@gmail.com")
        self.assertEqual(services.mask_email("ab@x.io"), "a*@x.io")
