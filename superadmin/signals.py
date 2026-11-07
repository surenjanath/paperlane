from django.contrib.auth import get_user_model
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver


def _meta(request):
    if request is None:
        return None, ""
    fwd = request.META.get("HTTP_X_FORWARDED_FOR")
    ip = (fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR")) or None
    return ip, (request.META.get("HTTP_USER_AGENT") or "")[:300]


def _record(request, result, user=None, email=""):
    from .models import SignInEvent

    ip, ua = _meta(request)
    try:
        SignInEvent.objects.create(user=user, email=(email or getattr(user, "email", ""))[:254], result=result, ip=ip, user_agent=ua)
    except Exception:  # never break signing in (e.g. before migrations)
        pass


@receiver(user_logged_in)
def _ok(sender, request, user, **kw):
    _record(request, "ok", user)


@receiver(user_logged_out)
def _out(sender, request, user, **kw):
    if user is not None:
        _record(request, "logout", user)


@receiver(user_login_failed)
def _failed(sender, credentials, request=None, **kw):
    ident = str((credentials or {}).get("username") or (credentials or {}).get("email") or "")[:254]
    user = get_user_model().objects.filter(email__iexact=ident).first() or get_user_model().objects.filter(username__iexact=ident).first()
    _record(request, "failed", user, user.email if user else ident)
