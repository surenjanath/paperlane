"""Platform rules applied to every request: maintenance mode, suspended workspaces, idle sign-out and required 2FA."""
import time

from django.contrib import messages
from django.contrib.auth import logout
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse

from .models import PlatformSettings, get_settings

ALWAYS_OPEN = ("/static/", "/media/", "/admin/", "/platform/", "/accounts/login/", "/accounts/logout/",
               "/accounts/password", "/favicon")
API_PREFIXES = ("/api/", "/mcp")


def needs_2fa(user, membership, ps):
    if ps.require_2fa == PlatformSettings.TWOFA_ALL:
        return True
    if ps.require_2fa == PlatformSettings.TWOFA_ADMINS:
        return user.is_superuser or bool(membership and membership.can_manage)
    return False


class PlatformMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        ps = get_settings()
        request.platform = ps
        path = request.path
        user = getattr(request, "user", None)
        signed_in = bool(user and user.is_authenticated)
        is_super = signed_in and user.is_superuser
        open_path = path.startswith(ALWAYS_OPEN)

        # 1. maintenance mode — super admins keep working
        if ps.maintenance_mode and not is_super and not open_path:
            if path.startswith(API_PREFIXES):
                return JsonResponse({"error": ps.maintenance_message or "Maintenance"}, status=503)
            resp = render(request, "superadmin/maintenance.html", {"ps": ps}, status=503)
            resp["Retry-After"] = "300"
            return resp

        if signed_in and not open_path:
            # 2. idle sign-out
            if ps.session_idle_minutes:
                now = int(time.time())
                last = request.session.get("last_seen")
                if last and now - last > ps.session_idle_minutes * 60:
                    logout(request)
                    messages.info(request, "You were signed out after a period of inactivity.")
                    return redirect(f"{reverse('accounts:login')}?next={path}")
                if not last or now - last > 30:  # write the session at most every 30 s
                    request.session["last_seen"] = now
            # 3. suspended workspace
            ws = getattr(request, "workspace", None)
            if ws is not None and ws.suspended_at and not is_super and not path.startswith(("/accounts/", "/account/")):
                if path.startswith(API_PREFIXES):
                    return JsonResponse({"error": "This workspace is suspended."}, status=403)
                return render(request, "superadmin/suspended.html", {"ws": ws, "ps": ps}, status=403)
            # 4. required two-step verification
            profile = getattr(request, "profile", None)
            if (profile is not None and not profile.two_factor_on and needs_2fa(user, getattr(request, "membership", None), ps)
                    and not path.startswith(("/account/security", "/accounts/")) and not path.startswith(API_PREFIXES)):
                messages.warning(request, "Your organisation requires two-step verification. Set it up to continue.")
                return redirect("accounts:security")
        return self.get_response(request)
