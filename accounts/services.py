"""
Email OTP issuing / verification.

Flow: register -> `issue_otp()` mails a 6-digit code -> the user types it on
the verify screen -> `verify_otp()` checks it. A code expires after
OTP_TTL_MINUTES, locks after OTP_MAX_ATTEMPTS wrong guesses, and new codes
are throttled (cooldown between sends + a per-hour cap) so the endpoint
can't be used to spam an inbox.
"""
import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from .models import EmailOTP

OTP_LENGTH = 6
OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_COOLDOWN_SECONDS = 60
OTP_MAX_SENDS_PER_HOUR = 5

OK, INVALID, EXPIRED, LOCKED, MISSING = "ok", "invalid", "expired", "locked", "missing"


class OTPThrottled(Exception):
    def __init__(self, retry_after_seconds):
        super().__init__(f"Try again in {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds


class OTPDeliveryError(Exception):
    pass


def _hash_code(user_id, code):
    key = settings.SECRET_KEY.encode()
    return hmac.new(key, f"{user_id}:{code}".encode(), hashlib.sha256).hexdigest()


def _generate_code():
    return "".join(str(secrets.randbelow(10)) for _ in range(OTP_LENGTH))


def _check_throttle(user):
    now = timezone.now()
    recent = user.email_otps.filter(created_at__gte=now - timedelta(hours=1))
    latest = recent.first()
    if latest:
        wait = OTP_RESEND_COOLDOWN_SECONDS - (now - latest.created_at).total_seconds()
        if wait > 0:
            raise OTPThrottled(int(wait) + 1)
    if recent.count() >= OTP_MAX_SENDS_PER_HOUR:
        oldest = recent.last()
        wait = 3600 - (now - oldest.created_at).total_seconds()
        raise OTPThrottled(int(max(wait, 1)))


def issue_otp(user):
    """Create + email a fresh OTP. Raises OTPThrottled / OTPDeliveryError."""
    _check_throttle(user)

    # only the newest code is ever valid
    user.email_otps.filter(consumed_at__isnull=True).update(consumed_at=timezone.now())

    code = _generate_code()
    otp = EmailOTP.objects.create(
        user=user,
        code_hash=_hash_code(user.pk, code),
        expires_at=timezone.now() + timedelta(minutes=OTP_TTL_MINUTES),
    )
    try:
        send_mail(
            subject="Your SafeRoutes verification code",
            message=(
                f"Your SafeRoutes verification code is {code}.\n\n"
                f"It expires in {OTP_TTL_MINUTES} minutes. If you didn't try to "
                "create a SafeRoutes account, you can ignore this email."
            ),
            from_email=None,
            recipient_list=[user.email],
        )
    except Exception as exc:  # SMTP down, bad credentials, ...
        otp.delete()
        raise OTPDeliveryError(str(exc)) from exc
    return otp


def verify_otp(user, code):
    """Return one of OK / INVALID / EXPIRED / LOCKED / MISSING."""
    otp = user.email_otps.filter(consumed_at__isnull=True).first()
    if otp is None:
        return MISSING
    if otp.expires_at <= timezone.now():
        return EXPIRED
    if otp.attempts >= OTP_MAX_ATTEMPTS:
        return LOCKED

    candidate = _hash_code(user.pk, (code or "").strip())
    if not hmac.compare_digest(candidate, otp.code_hash):
        otp.attempts += 1
        otp.save(update_fields=["attempts"])
        return LOCKED if otp.attempts >= OTP_MAX_ATTEMPTS else INVALID

    otp.consumed_at = timezone.now()
    otp.save(update_fields=["consumed_at"])
    return OK


def attempts_left(user):
    otp = user.email_otps.filter(consumed_at__isnull=True).first()
    return max(OTP_MAX_ATTEMPTS - otp.attempts, 0) if otp else 0


def mask_email(email):
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        masked = local[:1] + "*"
    else:
        masked = local[0] + "*" * (len(local) - 2) + local[-1]
    return f"{masked}@{domain}"
