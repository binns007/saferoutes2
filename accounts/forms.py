from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from .models import EmergencyContact
from .services import OTP_LENGTH

User = get_user_model()


class RegisterForm(forms.Form):
    email = forms.EmailField(max_length=150, widget=forms.EmailInput(attrs={
        "autocomplete": "email", "autofocus": True, "placeholder": "you@example.com"}))
    password1 = forms.CharField(label="Password", strip=False, widget=forms.PasswordInput(
        attrs={"autocomplete": "new-password"}))
    password2 = forms.CharField(label="Confirm password", strip=False, widget=forms.PasswordInput(
        attrs={"autocomplete": "new-password"}))

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()

    def clean(self):
        cleaned = super().clean()
        email, p1, p2 = cleaned.get("email"), cleaned.get("password1"), cleaned.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "The two passwords don't match.")
        elif p1 and email:
            try:
                validate_password(p1, user=User(username=email, email=email))
            except ValidationError as exc:
                self.add_error("password1", exc)
        return cleaned


class LoginForm(forms.Form):
    email = forms.EmailField(max_length=150, widget=forms.EmailInput(attrs={
        "autocomplete": "email", "autofocus": True}))
    password = forms.CharField(strip=False, widget=forms.PasswordInput(
        attrs={"autocomplete": "current-password"}))

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


class OTPForm(forms.Form):
    code = forms.RegexField(
        regex=rf"^\d{{{OTP_LENGTH}}}$",
        label="Verification code",
        error_messages={"invalid": f"Enter the {OTP_LENGTH}-digit code from your email."},
        widget=forms.TextInput(attrs={
            "inputmode": "numeric", "autocomplete": "one-time-code",
            "maxlength": OTP_LENGTH, "pattern": rf"\d{{{OTP_LENGTH}}}",
            "autofocus": True, "class": "otp-input", "placeholder": "••••••",
        }),
    )

    def clean_code(self):
        return self.cleaned_data["code"].strip()


class EmergencyContactForm(forms.ModelForm):
    confirm = forms.BooleanField(
        label="I've told this person they may receive an email from SafeRoutes if I need help.",
        error_messages={"required": "Please confirm you've told this person."},
    )

    class Meta:
        model = EmergencyContact
        fields = ["name", "email", "auto_escalate"]
        labels = {
            "name": "Contact's name",
            "email": "Contact's email",
            "auto_escalate": "If I don't respond to a safety alert within 60 seconds, notify them automatically",
        }
        widgets = {"name": forms.TextInput(attrs={"autocomplete": "off"}),
                   "email": forms.EmailInput(attrs={"autocomplete": "off"})}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if self.user and email == (self.user.email or "").lower():
            raise ValidationError("Choose someone other than yourself.")
        return email
