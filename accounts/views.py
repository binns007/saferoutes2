from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import check_password, make_password
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from . import services, throttle
from .forms import EmergencyContactForm, LoginForm, OTPForm, RegisterForm
from .models import EmergencyContact

User = get_user_model()

PENDING_SESSION_KEY = "pending_verification_user_id"
_DUMMY_HASH = make_password("not-a-real-password")


def _client_ip(request):
    return request.META.get("REMOTE_ADDR", "")


def _safe_next(request, default="dashboard:home"):
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return default


def _pending_user(request):
    user_id = request.session.get(PENDING_SESSION_KEY)
    if not user_id:
        return None
    return User.objects.filter(pk=user_id, is_active=False).first()


def _send_otp_or_message(request, user):
    """Issue an OTP, translating failures into user-facing messages. True on success."""
    try:
        services.issue_otp(user)
    except services.OTPThrottled as exc:
        messages.warning(
            request, f"A code was sent recently. You can request another in {exc.retry_after_seconds}s."
        )
        return True  # a valid code is already on its way; still show the verify screen
    except services.OTPDeliveryError:
        messages.error(request, "We couldn't send the verification email. Please try again shortly.")
        return False
    return True


def register(request):
    if request.user.is_authenticated:
        return redirect("dashboard:home")

    form = RegisterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        password = form.cleaned_data["password1"]

        existing = User.objects.filter(email__iexact=email).first()
        if existing and (existing.is_active or existing.is_staff):
            form.add_error("email", "An account with this email already exists. Try logging in.")
        else:
            if existing:  # unverified earlier attempt: let them restart with a new password
                user = existing
                user.set_password(password)
                user.save(update_fields=["password"])
            else:
                user = User.objects.create_user(
                    username=email, email=email, password=password, is_active=False
                )
            if _send_otp_or_message(request, user):
                request.session[PENDING_SESSION_KEY] = user.pk
                request.session["pending_next"] = _safe_next(request)
                return redirect("accounts:verify")
    return render(request, "accounts/register.html", {"form": form})


def verify(request):
    user = _pending_user(request)
    if user is None:
        messages.info(request, "Start by creating an account.")
        return redirect("accounts:register")

    form = OTPForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        result = services.verify_otp(user, form.cleaned_data["code"])
        if result == services.OK:
            user.is_active = True
            user.save(update_fields=["is_active"])
            next_url = request.session.pop("pending_next", "dashboard:home")
            request.session.pop(PENDING_SESSION_KEY, None)
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            messages.success(request, "Email verified — welcome to SafeRoutes.")
            return redirect(next_url)
        if result == services.INVALID:
            left = services.attempts_left(user)
            form.add_error("code", f"That code isn't right. {left} attempt{'s' if left != 1 else ''} left.")
        elif result == services.LOCKED:
            form.add_error("code", "Too many wrong attempts. Request a new code below.")
        elif result == services.EXPIRED:
            form.add_error("code", "That code has expired. Request a new code below.")
        else:
            form.add_error("code", "No active code. Request a new one below.")

    return render(request, "accounts/verify.html", {
        "form": form,
        "masked_email": services.mask_email(user.email),
        "ttl": services.OTP_TTL_MINUTES,
        "cooldown": services.OTP_RESEND_COOLDOWN_SECONDS,
    })


@require_POST
def resend(request):
    user = _pending_user(request)
    if user is None:
        return redirect("accounts:register")
    try:
        services.issue_otp(user)
        messages.success(request, "A new code is on its way.")
    except services.OTPThrottled as exc:
        messages.warning(request, f"Please wait {exc.retry_after_seconds}s before requesting another code.")
    except services.OTPDeliveryError:
        messages.error(request, "We couldn't send the verification email. Please try again shortly.")
    return redirect("accounts:verify")


def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard:home")

    form = LoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        password = form.cleaned_data["password"]
        ip = _client_ip(request)

        if throttle.is_blocked(email, ip):
            form.add_error(None, "Too many failed attempts. Please wait a few minutes and try again.")
        else:
            user = User.objects.filter(email__iexact=email).first()
            password_ok = bool(user and user.check_password(password))
            if not user:
                check_password(password, _DUMMY_HASH)  # keep timing similar for unknown emails
            if not password_ok:
                throttle.record_failure(email, ip)
                form.add_error(None, "Incorrect email or password.")
            elif not user.is_active:
                # right password but the email was never verified: resume verification
                if _send_otp_or_message(request, user):
                    request.session[PENDING_SESSION_KEY] = user.pk
                    request.session["pending_next"] = _safe_next(request)
                    messages.info(request, "Please verify your email to finish signing in.")
                    return redirect("accounts:verify")
            else:
                throttle.clear(email, ip)
                login(request, user, backend="django.contrib.auth.backends.ModelBackend")
                return redirect(_safe_next(request))

    return render(request, "accounts/login.html", {
        "form": form, "next": request.GET.get("next", ""),
    })


@require_POST
def logout_view(request):
    logout(request)
    messages.info(request, "You've been signed out.")
    return redirect("dashboard:home")


@login_required
def emergency_contact(request):
    """Set, change or remove the one person to notify if the user asks for help."""
    current = EmergencyContact.objects.filter(user=request.user).first()
    # Read this now: ModelForm validation mutates `current` in place with the submitted values.
    previous_email = current.email if current else None

    if request.method == "POST" and "remove" in request.POST:
        if current:
            current.delete()
            messages.info(request, "Emergency contact removed. SafeRoutes won't email anyone on your behalf.")
        return redirect("accounts:emergency_contact")

    form = EmergencyContactForm(request.POST or None, instance=current, user=request.user)
    if request.method == "POST" and form.is_valid():
        contact = form.save(commit=False)
        contact.user = request.user
        if contact.email != previous_email:
            # the contact is told before anything is saved; if that fails, nothing changes
            try:
                send_mail(
                    subject=f"{request.user.email} named you as their SafeRoutes emergency contact",
                    message=(
                        f"Hello {contact.name},\n\n{request.user.email} added you as their emergency contact on "
                        "SafeRoutes, a route-safety app.\n\nYou will only get an email if they press a button asking "
                        "SafeRoutes to let you know they may need help, or, if they turned it on, when a safety alert "
                        "goes unanswered for a minute. The email would say where they were last seen.\n\n"
                        "If you didn't expect this, you can ignore this email or ask them to remove you."
                    ),
                    from_email=None, recipient_list=[contact.email],
                )
            except Exception:
                form.add_error(None, "We couldn't email your contact to let them know, so nothing was saved. Please try again.")
                return render(request, "accounts/emergency_contact.html", {"form": form, "current": current})
        contact.save()
        messages.success(request, f"{contact.name} will be notified if you ask for help.")
        return redirect("accounts:emergency_contact")

    return render(request, "accounts/emergency_contact.html", {"form": form, "current": current})
