import functools


from accounts.models import Membership


@functools.cache
def _release_asset_version():
    """Production: computed once per process, so every deploy that changes a static file gets a new token."""
    return _asset_version()


def _asset_version():
    """Cache-busting token: newest modification time of static/ files."""
    import os

    from django.conf import settings

    newest = 0
    for root in settings.STATICFILES_DIRS:
        for dirpath, _, files in os.walk(root):
            for f in files:
                newest = max(newest, int(os.path.getmtime(os.path.join(dirpath, f))))
    return str(newest)


def shell(request):
    """Data for the app shell (sidebar, switcher, badges)."""
    from django.conf import settings

    from django.utils import timezone

    from superadmin.models import FEATURE_KEYS, get_settings

    asset_v = _asset_version() if settings.DEBUG else (getattr(settings, "ASSET_VERSION", "") or _release_asset_version())
    ps = getattr(request, "platform", None) or get_settings()
    announce = None
    if ps.announcement and (not ps.announcement_until or ps.announcement_until > timezone.now()):
        announce = {"text": ps.announcement, "level": ps.announcement_level, "key": f"{ps.pk}-{ps.updated_at:%s}"}
    from django.utils.functional import SimpleLazyObject

    from .help_views import _ai_ok

    base = {"assistant_ai": SimpleLazyObject(lambda: _ai_ok(request)), "list_view": "compact" if request.COOKIES.get("dl_view") == "compact" else "cards",
            "app_name": ps.site_name or "Paperlane", "asset_v": asset_v, "platform": ps, "announcement": announce,
            "maintenance_on": ps.maintenance_mode}
    user = getattr(request, "user", None)
    ws = getattr(request, "workspace", None)
    if not user or not user.is_authenticated or ws is None:
        return base

    from .models import Notification, RecentItem

    recents = list(RecentItem.objects.filter(user=user, workflow__workspace=ws, workflow__trashed_at__isnull=True)
                   .select_related("workflow")[:12])
    from .queries import waiting_packet_count

    waiting = waiting_packet_count(user, ws)
    return {
        **base,
        "features": {k: ws.feature(k) for k in FEATURE_KEYS},
        "my_workspaces": Membership.objects.filter(user=user, active=True).select_related("workspace"),
        "recents": recents,
        "unread_count": Notification.objects.filter(user=user, read=False).count(),
        "waiting_count": waiting,
        **_ticket_counts(request, ws),
    }


def _ticket_counts(request, ws):
    from flows.models import SupportTicket

    m = getattr(request, "membership", None)
    if m is None:
        return {}
    qs = SupportTicket.objects.filter(workspace=ws, status__in=SupportTicket.ACTIVE)
    if not (m.can_manage or m.role == "auditor"):
        qs = qs.filter(created_by=request.user)
    return {"open_tickets": qs.count(), "open_tickets_urgent": qs.filter(severity__in=["high", "critical"]).exists()}
