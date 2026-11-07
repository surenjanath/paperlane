from django.shortcuts import redirect
from django.urls import reverse

from .models import Membership, Profile

# Paths that never require an active workspace.
OPEN_PREFIXES = ("/accounts/", "/admin/", "/platform/", "/static/", "/media/", "/f/", "/start/", "/hooks/", "/invite/", "/api/", "/mcp", "/form/")


class WorkspaceMiddleware:
    """Attaches ``request.workspace`` and ``request.membership`` for signed-in users."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.workspace = None
        request.membership = None
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            profile, _ = Profile.objects.get_or_create(user=user)
            user.profile = profile  # reuse this object for request.user.profile (saves a query per access)
            request.profile = profile
            membership = None
            if profile.current_workspace_id:
                membership = Membership.objects.filter(
                    user=user, workspace_id=profile.current_workspace_id, active=True
                ).select_related("workspace").first()
            if membership is None:
                membership = Membership.objects.filter(user=user, active=True).select_related("workspace").first()
                if membership:
                    profile.current_workspace = membership.workspace
                    profile.save(update_fields=["current_workspace"])
            if "meta" not in request.session:  # device info for My account › Security (older sessions)
                from .security_views import note_session

                note_session(request)
            if membership:
                request.membership = membership
                request.workspace = membership.workspace
            elif not request.path.startswith(OPEN_PREFIXES) and request.path != reverse("accounts:onboarding"):
                return redirect("accounts:onboarding")
        return self.get_response(request)
