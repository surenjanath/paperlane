"""My account › Security (two-factor sign-in, active sessions) and API tokens, plus the two-factor login step."""
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from flows.engine import client_ip

from . import twofactor
from .models import ApiToken, Membership, Profile

User = get_user_model()
PENDING = "2fa_pending"
VERIFY_MAX_FAILURES = 6


def _profile(user):
    return Profile.objects.get_or_create(user=user)[0]


def note_session(request):
    """Remember where a session was opened (shown on the Security page)."""
    ua = request.META.get("HTTP_USER_AGENT", "")[:200]
    request.session["meta"] = {"ip": client_ip(request), "ua": ua, "at": timezone.now().isoformat(timespec="seconds")}


def finish_login(request, user, remember, nxt):
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    if not remember:
        request.session.set_expiry(0)  # ends when the browser closes
    note_session(request)
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(nxt)
    return redirect("core:dashboard")


def start_two_factor(request, user, remember, nxt):
    """Password was right and two-factor is on: park the sign-in until a code is entered."""
    request.session[PENDING] = {"user": user.pk, "remember": bool(remember), "next": nxt,
                                "at": timezone.now().timestamp()}
    return redirect("accounts:login_verify")


def login_verify(request):
    pending = request.session.get(PENDING)
    if not pending or timezone.now().timestamp() - pending.get("at", 0) > 600:
        request.session.pop(PENDING, None)
        messages.error(request, "Your sign-in expired. Log in again.")
        return redirect("accounts:login")
    user = User.objects.filter(pk=pending["user"], is_active=True).first()
    if user is None:
        return redirect("accounts:login")
    error = ""
    key = f"2fa-fail:{user.pk}"
    if request.method == "POST":
        if cache.get(key, 0) >= VERIFY_MAX_FAILURES:
            request.session.pop(PENDING, None)
            messages.error(request, "Too many incorrect codes. Log in again in 15 minutes.")
            return redirect("accounts:login")
        how = twofactor.verify(_profile(user), request.POST.get("code", ""))
        if how:
            cache.delete(key)
            request.session.pop(PENDING, None)
            if how == "recovery":
                left = len(user.profile.recovery_codes or [])
                messages.warning(request, f"You used a recovery code — {left} left. Generate new ones in My account › Security.")
            return finish_login(request, user, pending["remember"], pending["next"])
        cache.set(key, cache.get(key, 0) + 1, 15 * 60)
        error = "That code isn't right. Check your authenticator app and try again."
        from superadmin.signals import _record

        _record(request, "failed", user, f"{user.email} (two-step code)")
    return render(request, "accounts/login_verify.html", {"error": error, "email": user.email,
                                                           "use_recovery": request.GET.get("recovery") == "1"})


# ---- My account › Security --------------------------------------------------------------------
def _sessions(request):
    rows = []
    for s in Session.objects.filter(expire_date__gt=timezone.now()):
        try:
            data = s.get_decoded()
        except Exception:
            continue
        if str(data.get("_auth_user_id")) != str(request.user.pk):
            continue
        meta = data.get("meta") or {}
        ua = meta.get("ua", "")
        browser = next((b for b in ("Edg", "Chrome", "Firefox", "Safari") if b in ua), "Unknown browser")
        device = next((d for d in ("iPhone", "iPad", "Android", "Macintosh", "Windows", "Linux") if d in ua), "")
        rows.append({"key": s.session_key, "current": s.session_key == request.session.session_key,
                     "label": f"{ {'Edg': 'Edge'}.get(browser, browser)}{' on ' + device.replace('Macintosh', 'Mac') if device else ''}",
                     "ip": meta.get("ip", "—"), "since": parse_datetime(meta["at"]) if meta.get("at") else None,
                     "expires": s.expire_date})
    rows.sort(key=lambda r: (not r["current"], -(r["since"].timestamp() if r["since"] else 0)))
    return rows


@login_required
def security(request):
    p = _profile(request.user)
    ctx = {"section": "security", "profile": p, "sessions": _sessions(request),
           "recovery_left": len(p.recovery_codes or [])}
    setup = request.session.get("2fa_setup")
    if setup and not p.two_factor_on:
        ctx["setup_secret"] = setup
        ctx["setup_qr"] = twofactor.qr_svg(twofactor.otpauth_uri(setup, request.user.email))
    fresh = request.session.pop("2fa_new_codes", None)
    if fresh:
        ctx["new_codes"] = fresh
    return render(request, "accounts/security.html", ctx)


@login_required
@require_POST
def security_action(request, action):
    p = _profile(request.user)
    back = redirect("accounts:security")
    if action == "2fa-start":
        if not p.two_factor_on:
            request.session["2fa_setup"] = twofactor.new_secret()
        return back
    if action == "2fa-cancel":
        request.session.pop("2fa_setup", None)
        return back
    if action == "2fa-confirm":
        secret = request.session.get("2fa_setup")
        step = twofactor.match_step(secret, request.POST.get("code"))
        if not secret or step is None:
            messages.error(request, "That code didn't match. Scan the QR code again and enter the current 6-digit code.")
            return back
        plain, hashes = twofactor.new_recovery_codes()
        p.totp_secret, p.totp_enabled_at, p.totp_last_step, p.recovery_codes = secret, timezone.now(), step, hashes
        p.save(update_fields=["totp_secret", "totp_enabled_at", "totp_last_step", "recovery_codes"])
        request.session.pop("2fa_setup", None)
        request.session["2fa_new_codes"] = plain
        messages.success(request, "Two-factor sign-in is on.")
        return back
    if action in ("2fa-disable", "2fa-codes"):
        if not request.user.check_password(request.POST.get("password", "")):
            messages.error(request, "Password is incorrect.")
            return back
        if action == "2fa-disable":
            p.totp_secret, p.totp_enabled_at, p.recovery_codes, p.totp_last_step = "", None, [], 0
            p.save(update_fields=["totp_secret", "totp_enabled_at", "recovery_codes", "totp_last_step"])
            messages.success(request, "Two-factor sign-in is off.")
        elif p.two_factor_on:
            plain, p.recovery_codes = twofactor.new_recovery_codes()
            p.save(update_fields=["recovery_codes"])
            request.session["2fa_new_codes"] = plain
            messages.success(request, "New recovery codes generated — the old ones no longer work.")
        return back
    if action == "sign-out-session":
        key = request.POST.get("key", "")
        if key and key != request.session.session_key and any(r["key"] == key for r in _sessions(request)):
            Session.objects.filter(session_key=key).delete()
            messages.success(request, "Signed out of that session.")
        return back
    if action == "sign-out-others":
        n = 0
        for r in _sessions(request):
            if not r["current"]:
                Session.objects.filter(session_key=r["key"]).delete()
                n += 1
        messages.success(request, f"Signed out of {n} other session{'s' if n != 1 else ''}.")
        return back
    return back


# ---- My account › API tokens -------------------------------------------------------------------
@login_required
def api_tokens(request):
    ws = request.workspace
    from superadmin.features import feature_off

    if (off := feature_off(request, "api")) is not None:
        return off
    if request.method == "POST":
        if request.POST.get("action") == "revoke":
            tok = ApiToken.objects.filter(user=request.user, pk=request.POST.get("token")).first()
            if tok and not tok.revoked_at:
                tok.revoked_at = timezone.now()
                tok.save(update_fields=["revoked_at"])
                messages.success(request, f"Revoked “{tok.name}”.")
            return redirect("accounts:api_tokens")
        if ws is None or not Membership.objects.filter(user=request.user, workspace=ws, active=True).exists():
            messages.error(request, "Open a workspace first.")
            return redirect("accounts:api_tokens")
        if ApiToken.objects.filter(user=request.user, revoked_at__isnull=True).count() >= 20:
            messages.error(request, "You have 20 active tokens. Revoke one first.")
            return redirect("accounts:api_tokens")
        scope = ApiToken.READ if request.POST.get("scope") == "read" else ApiToken.FULL
        days = request.POST.get("days") or None
        days = int(days) if days and days.isdigit() and 0 < int(days) <= 366 else None
        tok, raw = ApiToken.issue(request.user, ws, request.POST.get("name", "").strip() or "API token", scope, days)
        request.session["new_api_token"] = {"id": tok.pk, "raw": raw}
        messages.success(request, "Token created. Copy it now — it won't be shown again.")
        return redirect("accounts:api_tokens")
    new = request.session.pop("new_api_token", None)
    base = request.build_absolute_uri("/").rstrip("/")
    return render(request, "accounts/api_tokens.html", {
        "section": "api", "tokens": ApiToken.objects.filter(user=request.user).select_related("workspace"),
        "new_token": new, "base_url": base, "mcp_url": base + reverse("mcp_http"),
    })


# ---- My account › Out of office ----------------------------------------------------------------
@login_required
def out_of_office(request):
    import datetime

    p = _profile(request.user)
    ws = request.workspace
    teammates = (Membership.objects.filter(workspace=ws, active=True).exclude(user=request.user).select_related("user")
                 .order_by("user__first_name", "user__email")) if ws else []
    if request.method == "POST":
        if request.POST.get("action") == "off":
            p.ooo_from = p.ooo_until = None
            p.ooo_delegate = None
            p.save(update_fields=["ooo_from", "ooo_until", "ooo_delegate"])
            messages.success(request, "Out of office is off. New work comes to you again.")
            nxt = request.POST.get("next") or ""
            if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
                return redirect(nxt)
            return redirect("accounts:out_of_office")
        try:
            start = datetime.date.fromisoformat(request.POST.get("ooo_from") or timezone.localdate().isoformat())
            until = datetime.date.fromisoformat(request.POST.get("ooo_until", ""))
        except ValueError:
            messages.error(request, "Choose when you're back.")
            return redirect("accounts:out_of_office")
        if until < start or until < timezone.localdate():
            messages.error(request, "The return date must be today or later, and not before the start date.")
            return redirect("accounts:out_of_office")
        delegate = next((m.user for m in teammates if str(m.user_id) == request.POST.get("delegate")), None)
        if delegate is None:
            messages.error(request, "Choose a teammate to cover for you.")
            return redirect("accounts:out_of_office")
        p.ooo_from, p.ooo_until, p.ooo_delegate = start, until, delegate
        p.ooo_message = request.POST.get("ooo_message", "").strip()[:240]
        p.save(update_fields=["ooo_from", "ooo_until", "ooo_delegate", "ooo_message"])
        from flows import engine

        engine.notify(delegate, ws, f"You're covering for {request.user.get_full_name() or request.user.email}",
                      f"{start:%b %d} – {until:%b %d}: their new steps come to you.", "/inbox/", kind="team")
        messages.success(request, f"Out of office is set. New steps go to {delegate.get_full_name() or delegate.email} until {until:%b %d}.")
        return redirect("accounts:out_of_office")
    from core.queries import covering_for

    covering = Profile.objects.filter(user_id__in=covering_for(request.user)).select_related("user")
    return render(request, "accounts/out_of_office.html", {"section": "ooo", "profile": p, "teammates": teammates,
                                                           "today": timezone.localdate(), "covering": covering})
