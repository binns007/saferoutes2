from django.conf import settings
from django.db import models


class EmailOTP(models.Model):
    """
    One-time passcode emailed to a user to prove they own the address they
    registered with. Only a keyed hash of the code is stored, never the code
    itself, so a database leak doesn't reveal usable codes.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="email_otps"
    )
    code_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self):
        return f"OTP for {self.user_id} @ {self.created_at:%Y-%m-%d %H:%M}"


class EmergencyContact(models.Model):
    """
    The one person a user has chosen to be told if they need help.

    Strictly opt-in: the user must confirm they've told this person, the
    contact is emailed when added, and SafeRoutes only emails them when the
    user presses "Notify my emergency contact" -- or, if the user ticked
    `auto_escalate`, when a safety alert goes unanswered for a visible
    countdown. The contact's address is never sent to the browser.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="emergency_contact"
    )
    name = models.CharField(max_length=100)
    email = models.EmailField()
    auto_escalate = models.BooleanField(
        default=False,
        help_text="Notify the contact if a safety alert is left unanswered for the countdown.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} <{self.email}> for user {self.user_id}"
