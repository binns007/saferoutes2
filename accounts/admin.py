from django.contrib import admin

from .models import EmailOTP, EmergencyContact


@admin.register(EmailOTP)
class EmailOTPAdmin(admin.ModelAdmin):
    # the code itself is never stored, only a keyed hash
    list_display = ("user", "created_at", "expires_at", "attempts", "consumed_at")
    readonly_fields = ("user", "code_hash", "created_at", "expires_at", "attempts", "consumed_at")


@admin.register(EmergencyContact)
class EmergencyContactAdmin(admin.ModelAdmin):
    list_display = ("user", "name", "auto_escalate", "created_at")
    search_fields = ("user__email", "name")
