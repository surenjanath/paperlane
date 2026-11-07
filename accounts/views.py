"""Authentication, onboarding, My account, My workspace, Team management and Contacts."""
import re
import csv
import io

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from core.utils import safe_next
from flows import engine, mailer
from flows.models import AuditEvent, DataSheet, OutboundEmail

from .forms import ContactForm, LoginForm, ProfileForm, SignupForm, WorkspaceCreateForm, WorkspaceInfoForm
from .models import (Connection, Contact, EmailTemplate, Group, Invitation, Membership, Profile, UsageEvent,
                     Workspace, default_thank_you, default_workspace_settings)

User = get_user_model()


def manager_required(view):
    def wrapped(request, *args, **kwargs):
        if not request.membership or not request.membership.can_manage:
            raise PermissionDenied("Only workspace owners and supervisors can do this.")
        return view(request, *args, **kwargs)

    wrapped.__name__ = view.__name__
    return login_required(wrapped)


# ---- auth ------------------------------------------------------------------------------------
def login_view(request):
    if request.user.is_authenticated and not (settings.DEBUG and request.GET.get("preview")):
        return redirect("core:dashboard")
    from django.core.cache import cache

    from flows.engine import client_ip

    form = LoginForm(request.POST or None, initial={"remember": True})
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"].lower()
        keys = [f"login-fail:email:{email}", f"login-fail:ip:{client_ip(request)}"]
        if any(cache.get(k, 0) >= LOGIN_MAX_FAILURES for k in keys):
            form.add_error(None, "Too many failed attempts. Wait 15 minutes or reset your password.")
            return render(request, "accounts/login.html", {"form": form}, status=429)
        account = User.objects.filter(email__iexact=email).first()
        user = authenticate(request, username=account.username if account else email, password=form.cleaned_data["password"])
        if user is None:
            for k in keys:
                cache.set(k, cache.get(k, 0) + 1, 15 * 60)
            form.add_error(None, "Incorrect email or password.")
        else:
            for k in keys:
                cache.delete(k)
            from .security_views import finish_login, start_two_factor

            remember, nxt = form.cleaned_data.get("remember"), request.GET.get("next") or ""
            profile = Profile.objects.get_or_create(user=user)[0]
            if profile.two_factor_on:
                return start_two_factor(request, user, remember, nxt)
            return finish_login(request, user, remember, nxt)
    from .models import SSOProvider

    return render(request, "accounts/login.html", {"form": form, "demo_accounts": demo_accounts(),
                                                   "sso": SSOProvider.objects.filter(enabled=True)})


LOGIN_MAX_FAILURES = 8

# One-click demo sign-in (development only): the seeded demo users, one per role.
DEMO_USERS = [("owner@paperlane.test", "Full control: settings, billing, team"), ("sam@paperlane.test", "Manages people, workflows, every document"),
              ("ana@paperlane.test", "Read-only view of everything + audit"), ("cal@paperlane.test", "Builds workflows and sends documents"),
              ("fay@paperlane.test", "Fills documents and runs shared workflows")]


def demo_login_enabled():
    """Never in production: only with DEBUG on, or when PAPERLANE_DEMO_LOGIN=1 is set explicitly."""
    import os

    return settings.DEBUG or os.environ.get("PAPERLANE_DEMO_LOGIN") == "1"


def demo_accounts():
    if not demo_login_enabled():
        return []
    users = {u.email.lower(): u for u in User.objects.filter(email__in=[e for e, _ in DEMO_USERS], is_active=True)
             .prefetch_related("memberships")}
    out = []
    for email, blurb in DEMO_USERS:
        u = users.get(email)
        m = next((x for x in u.memberships.all() if x.active), None) if u else None
        if m:
            out.append({"email": email, "name": u.get_full_name() or email, "role": m.get_role_display(), "role_key": m.role, "blurb": blurb})
    return out


@require_POST
def demo_login(request):
    from django.http import Http404

    from .security_views import finish_login

    if not demo_login_enabled():
        raise Http404
    email = request.POST.get("email", "").lower()
    if email not in {e for e, _ in DEMO_USERS}:
        raise Http404
    user = User.objects.filter(email__iexact=email, is_active=True).first()
    if user is None:
        raise Http404
    return finish_login(request, user, True, request.GET.get("next") or request.POST.get("next") or "")


def signup_view(request):
    if request.user.is_authenticated and not (settings.DEBUG and request.GET.get("preview")):
        return redirect("core:dashboard")
    from superadmin.models import PlatformSettings, get_settings

    ps = get_settings()
    invited = bool(request.session.get("invite_token"))
    if ps.signup_mode == PlatformSettings.SIGNUP_CLOSED or (ps.signup_mode == PlatformSettings.SIGNUP_INVITE and not invited):
        return render(request, "accounts/signup_closed.html", {"ps": ps, "invite_only": ps.signup_mode == PlatformSettings.SIGNUP_INVITE})
    initial = {"email": request.session.get("invite_email", "")}
    form = SignupForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid() and ps.signup_mode == PlatformSettings.SIGNUP_DOMAINS and not invited:
        domain = form.cleaned_data["email"].rsplit("@", 1)[-1].lower()
        if domain not in ps.domain_list():
            form.add_error("email", f"Sign-up is limited to {', '.join('@' + d for d in ps.domain_list()) or 'approved domains'}.")
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        base = d["email"].split("@")[0][:140]
        username, n = base, 1
        while User.objects.filter(username__iexact=username).exists():
            n += 1
            username = f"{base}{n}"
        user = User.objects.create_user(username=username, email=d["email"], password=d["password"],
                                        first_name=d["first_name"], last_name=d["last_name"])
        Profile.objects.get_or_create(user=user)
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        from .security_views import note_session

        note_session(request)
        token = request.session.pop("invite_token", None)
        if token:
            return redirect("accept_invite", token)
        return redirect("accounts:onboarding")
    return render(request, "accounts/signup.html", {"form": form})


def logout_view(request):
    """POST logs out; a plain visit asks first (so a link or image elsewhere cannot sign people out)."""
    if request.method == "POST":
        logout(request)
        return redirect("accounts:login")
    if not request.user.is_authenticated:
        return redirect("accounts:login")
    return render(request, "accounts/logout_confirm.html")


@login_required
def onboarding(request):
    email_domain = request.user.email.split("@")[-1].lower() if request.user.email else ""
    joinable = [w for w in Workspace.objects.exclude(memberships__user=request.user)
                if w.setting("domain_join") and email_domain in w.domain_list()]
    invites = Invitation.objects.filter(email__iexact=request.user.email, accepted_at__isnull=True)
    form = WorkspaceCreateForm(request.POST or None)
    if request.method == "POST":
        if request.POST.get("join"):
            ws = get_object_or_404(Workspace, pk=request.POST["join"])
            if ws not in joinable:
                raise PermissionDenied
            Membership.objects.get_or_create(user=request.user, workspace=ws, defaults={"role": Membership.CREATOR})
            _switch(request, ws)
            return redirect("core:dashboard")
        from superadmin.models import get_settings

        ps = get_settings()
        owned = Membership.objects.filter(user=request.user, role=Membership.OWNER).count()
        if not request.user.is_superuser and not ps.feature("create_workspaces"):
            form.add_error(None, "Creating workspaces is turned off. Ask your administrator for an invitation.")
        elif not request.user.is_superuser and owned >= ps.max_workspaces_per_user:
            form.add_error(None, f"You can own at most {ps.max_workspaces_per_user} workspaces.")
        if form.is_valid():
            ws = form.save(commit=False)
            ws.plan_expires = (timezone.now() + timezone.timedelta(days=ps.default_plan_days)).date()
            ws.plan, ws.credits_limit = ps.default_plan, ps.default_credits
            ws.email = request.user.email
            ws.save()
            Membership.objects.create(user=request.user, workspace=ws, role=Membership.OWNER, position="Administrator")
            _switch(request, ws)
            from core.views import seed_widgets

            seed_widgets(request.user, ws)
            messages.success(request, f"Welcome to the {ws.name} workspace!")
            return redirect("core:dashboard")
    from superadmin.models import get_settings

    can_create = request.user.is_superuser or get_settings().feature("create_workspaces")
    return render(request, "accounts/onboarding.html", {"form": form, "joinable": joinable, "invites": invites,
                                                        "can_create": can_create})


def _switch(request, ws):
    profile = request.user.profile
    profile.current_workspace = ws
    profile.save(update_fields=["current_workspace"])


@login_required
@require_POST
def switch_workspace(request, pk):
    m = get_object_or_404(Membership, user=request.user, workspace_id=pk, active=True)
    _switch(request, m.workspace)
    return redirect("core:dashboard")


def accept_invite(request, token):
    inv = get_object_or_404(Invitation, token=token)
    if not request.user.is_authenticated:
        request.session["invite_token"] = token
        request.session["invite_email"] = inv.email
        if User.objects.filter(email__iexact=inv.email).exists():
            messages.info(request, "Log in to accept your invitation.")
            return redirect(reverse("accounts:login") + "?next=" + reverse("accept_invite", args=[token]))
        return redirect("accounts:signup")
    if request.user.email.lower() != inv.email.lower():
        messages.error(request, f"This invitation was sent to {inv.email}. Log in with that account to accept it.")
        return redirect("core:dashboard" if request.membership else "accounts:onboarding")
    Membership.objects.update_or_create(user=request.user, workspace=inv.workspace,
                                        defaults={"role": inv.role, "active": True})
    inv.accepted_at = timezone.now()
    inv.save(update_fields=["accepted_at"])
    _switch(request, inv.workspace)
    if inv.invited_by:
        engine.notify(inv.invited_by, inv.workspace, f"{engine.actor_label(request.user)} joined {inv.workspace.name}", kind="team")
    messages.success(request, f"You joined {inv.workspace.name}.")
    return redirect("core:dashboard")


# ---- My account ---------------------------------------------------------------------------------
@login_required
def profile(request):
    u, prof, m = request.user, request.user.profile, request.membership
    initial = {"first_name": u.first_name, "last_name": u.last_name, "username": u.username, "phone": prof.phone,
               "position": m.position if m else "", "fax": m.fax if m else "", "video_link": m.video_link if m else ""}
    form = ProfileForm(request.POST or None, request.FILES or None, initial=initial, user=u)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        u.first_name, u.last_name, u.username = d["first_name"], d["last_name"], d["username"]
        u.save()
        prof.phone = d["phone"]
        if d.get("avatar"):
            prof.avatar = d["avatar"]
        if request.POST.get("remove_avatar"):
            prof.avatar = ""
        prof.save()
        if m:
            m.position, m.fax, m.video_link = d["position"], d["fax"], d["video_link"]
            m.save()
        messages.success(request, "Profile updated.")
        return redirect("accounts:profile")
    return render(request, "accounts/profile.html", {"form": form, "section": "profile"})


@login_required
def account_settings(request):
    pw_form = PasswordChangeForm(request.user, request.POST if request.POST.get("form") == "password" else None)
    if request.method == "POST":
        which = request.POST.get("form")
        if which == "password" and pw_form.is_valid():
            pw_form.save()
            update_session_auth_hash(request, pw_form.user)
            messages.success(request, "Password changed.")
            return redirect("accounts:settings")
        if which == "email":
            email = request.POST.get("email", "").strip().lower()
            if email and not User.objects.filter(email__iexact=email).exclude(pk=request.user.pk).exists():
                request.user.email = email
                request.user.save(update_fields=["email"])
                messages.success(request, "Email updated.")
            else:
                messages.error(request, "That email can't be used.")
            return redirect("accounts:settings")
        if which == "language":
            request.user.profile.language = request.POST.get("language", "en")
            request.user.profile.save(update_fields=["language"])
            messages.success(request, "Language saved.")
            return redirect("accounts:settings")
        if which == "delete":
            if not request.user.check_password(request.POST.get("confirm_password", "")):
                messages.error(request, "Password is incorrect.")
                return redirect("accounts:settings")
            owned = Membership.objects.filter(user=request.user, role=Membership.OWNER)
            for om in owned:
                if not om.workspace.memberships.filter(role=Membership.OWNER).exclude(user=request.user).exists():
                    messages.error(request, f"Transfer ownership of {om.workspace.name} before deleting your account.")
                    return redirect("accounts:settings")
            user = request.user
            logout(request)
            user.delete()
            return redirect("accounts:login")
    return render(request, "accounts/account_settings.html", {"pw_form": pw_form, "section": "settings"})


@login_required
def notification_settings(request):
    prof = request.user.profile
    m = request.membership
    if request.method == "POST":
        if request.POST.get("form") == "preview":
            if request.POST.get("preview") not in ("digest", "team_report"):
                return redirect("accounts:notifications")
            from flows.digests import send_for

            kind = request.POST["preview"]
            if kind == "team_report" and not (m and m.can_view_all):
                raise PermissionDenied
            send_for(m, kind, timezone.now() - timezone.timedelta(days=7), force=True)
            messages.success(request, f"A preview was sent to {request.user.email}.")
            return redirect("accounts:notifications")
        # store only what differs from the defaults (on for in-app notifications, off for digests)
        prefs = {k: v for k, v in (prof.notification_prefs or {}).items() if ":" in k}  # digest bookkeeping
        prefs.update({k: False for k, _ in Profile.NOTIFICATION_KEYS if request.POST.get(k) != "on"})
        for kind in ("digest", "team_report"):
            v = request.POST.get(kind, "off")
            if v in ("daily", "weekly"):
                prefs[kind] = v
        prof.notification_prefs = prefs
        prof.save(update_fields=["notification_prefs"])
        messages.success(request, "Notification settings saved.")
        return redirect("accounts:notifications")
    items = [(k, label, prof.wants(k)) for k, label in Profile.NOTIFICATION_KEYS]
    prefs = prof.notification_prefs or {}
    return render(request, "accounts/notification_settings.html", {
        "items": items, "section": "notifications", "digest": prefs.get("digest", "off"),
        "team_report": prefs.get("team_report", "off"), "is_manager": bool(m and m.can_view_all)})


@login_required
def my_activity(request):
    events = AuditEvent.objects.filter(actor=request.user).select_related("workflow", "packet")[:300]
    return render(request, "accounts/activity.html", {"events": events, "section": "activity"})


@login_required
def connections(request):
    if request.method == "POST":
        if request.POST.get("delete"):
            Connection.objects.filter(user=request.user, pk=request.POST["delete"]).delete()
            messages.success(request, "Connection removed.")
        else:
            kind = request.POST.get("kind", "webhook")
            Connection.objects.create(user=request.user, workspace=request.workspace, kind=kind,
                                      label=request.POST.get("label") or dict(Connection.KINDS).get(kind, kind),
                                      endpoint=request.POST.get("endpoint", ""), secret=request.POST.get("secret", ""))
            messages.success(request, "Account connected.")
        return redirect("accounts:connections")
    from flows import clouddrive

    return render(request, "accounts/connections.html", {
        "items": Connection.objects.filter(workspace=request.workspace), "kinds": Connection.KINDS, "section": "connections",
        "oauth": [(p, label, clouddrive.configured(p)) for p, label in (("google", "Google Drive"), ("microsoft", "Microsoft 365"))]})


@login_required
def oauth_start(request, provider):
    from flows import clouddrive

    if provider not in ("google", "microsoft") or not clouddrive.configured(provider):
        messages.error(request, "This provider isn't set up on the server yet. Ask your administrator.")
        return redirect("accounts:connections")
    return redirect(clouddrive.auth_url(provider, request.user, request.workspace))


@login_required
def oauth_callback(request, provider):
    from django.core import signing

    from flows import clouddrive

    try:
        state = clouddrive.read_state(request.GET.get("state", ""))
    except signing.BadSignature:
        messages.error(request, "That sign-in link expired. Try connecting again.")
        return redirect("accounts:connections")
    if state.get("u") != request.user.pk or state.get("p") != provider or state.get("w") != request.workspace.pk:
        messages.error(request, "That sign-in was started by someone else.")
        return redirect("accounts:connections")
    if request.GET.get("error") or not request.GET.get("code"):
        messages.error(request, "The connection was cancelled.")
        return redirect("accounts:connections")
    try:
        tokens = clouddrive.exchange(provider, request.GET["code"])
    except clouddrive.DriveError as exc:
        messages.error(request, f"Couldn't connect: {exc}")
        return redirect("accounts:connections")
    label = "Google Drive" if provider == "google" else "Microsoft 365 (OneDrive / SharePoint)"
    Connection.objects.create(user=request.user, workspace=request.workspace, kind=provider, label=label, data=tokens)
    messages.success(request, f"{label} connected. Add a “Save to cloud drive” bot to file finished documents there.")
    return redirect("accounts:connections")


# ---- My workspace ------------------------------------------------------------------------------
@manager_required
def workspace_info(request):
    ws = request.workspace
    form = WorkspaceInfoForm(request.POST or None, request.FILES or None, instance=ws)
    if request.method == "POST":
        if request.POST.get("leave"):
            return _leave(request)
        if form.is_valid():
            form.save()
            messages.success(request, "Workspace information saved.")
            return redirect("accounts:workspace_info")
    return render(request, "accounts/ws_info.html", {"form": form, "section": "info"})


@login_required
@require_POST
def leave_workspace(request):
    return _leave(request)


def _leave(request):
    m = request.membership
    if m.role == Membership.OWNER and not m.workspace.memberships.filter(role=Membership.OWNER).exclude(pk=m.pk).exists():
        messages.error(request, "You're the only owner. Make someone else an owner before leaving.")
        return redirect("accounts:workspace_info")
    m.delete()
    messages.success(request, f"You left {m.workspace.name}.")
    return redirect("core:dashboard")


SETTINGS_LABELS = [
    ("Invitation settings", [
        ("domain_join", "Verified domains", "Allow anyone with a verified domain address to join your Workspace."),
        ("teammate_emails", "Email notifications to teammates", "Send an email to teammates when their Workspace access is activated or blocked."),
    ]),
    ("Workflow and template settings", [
        ("default_kind", "Workflow and template", "Create workflows by default."),
    ]),
    ("Recipient settings", [
        ("recipient_registration", "Recipient registration", "Allow recipients to create a Paperlane account once they have completed a document."),
        ("multiple_documents", "Fill out multiple documents at once", "Allow recipients to open the next document package directly from the Thank You page."),
        ("autofill_signatures", "Auto-fill signatures and initials", "Allow recipients to auto-fill all signature and initial fields with one click."),
        ("esign_consent", "eSignature consent", "Ask recipients for their consent to use legally-binding electronic signatures when filling out the documents."),
        ("certificate_of_completion", "Certificate of completion", "Attach a certificate of completion when exporting completed documents."),
        ("prefill_from_api", "Pre-fill from API", "Allow bots that pre-fill sent documents with API data."),
        ("client_portal", "Client portal", "Let recipients see every document waiting for them, and download the ones they finished, at /portal/<your workspace>/ after confirming their email address."),
    ]),
]


@manager_required
def workspace_settings(request):
    ws = request.workspace
    if request.method == "POST":
        merged = default_workspace_settings()
        merged.update(ws.settings or {})
        for _, items in SETTINGS_LABELS:
            for key, _, _ in items:
                if key == "default_kind":
                    merged[key] = "workflow" if request.POST.get(key) == "on" else "template"
                else:
                    merged[key] = request.POST.get(key) == "on"
        lang = request.POST.get("recipient_language", "en")
        if lang in ("es", "auto"):  # store only what differs from the default (English)
            merged["recipient_language"] = lang
        else:
            merged.pop("recipient_language", None)
        ws.settings = merged
        ws.verified_domains = request.POST.get("verified_domains", ws.verified_domains)
        ws.save(update_fields=["settings", "verified_domains"])
        messages.success(request, "Workspace settings saved.")
        return redirect("accounts:workspace_settings")
    groups = [(title, [(k, label, help, ws.setting(k) if k != "default_kind" else ws.setting(k) == "workflow")
                       for k, label, help in items]) for title, items in SETTINGS_LABELS]
    return render(request, "accounts/ws_settings.html", {"groups": groups, "section": "settings",
                                                         "recipient_language": (ws.settings or {}).get("recipient_language", "en")})


@manager_required
def retention_settings(request):
    from flows import retention

    ws = request.workspace
    if request.method == "POST":
        if request.membership.role != Membership.OWNER:
            raise PermissionDenied("Only workspace owners can change data retention.")

        def days(key):
            try:
                return max(0, min(36500, int(request.POST.get(key) or 0)))
            except ValueError:
                return 0
        statuses = [x for x in request.POST.getlist("statuses") if x in ("completed", "declined", "cancelled")] or ["completed"]
        new = {"documents_days": days("documents_days"), "statuses": statuses, "trash_days": days("trash_days"),
               "audit_days": days("audit_days")}
        ws.settings = {**(ws.settings or {}), "retention": new}
        ws.save(update_fields=["settings"])
        engine.audit(ws, "workspace.retention", str(new), actor=request.user, request=request)
        messages.success(request, "Retention rules saved. They run once a day.")
        return redirect("accounts:retention")
    from flows.models import Packet

    return render(request, "accounts/ws_retention.html", {
        "section": "retention", "p": retention.policy(ws), "preview": retention.preview(ws),
        "held": Packet.objects.filter(workspace=ws, flags__legal_hold=True).count(),
        "is_owner": request.membership.role == Membership.OWNER})


@manager_required
def theme(request):
    from .models import THEME_PRESETS, default_theme

    ws = request.workspace
    fonts = ["Plus Jakarta Sans", "Inter", "Roboto", "Open Sans", "Lato", "Montserrat", "Poppins", "Nunito", "Merriweather", "Source Serif 4", "Playfair Display"]
    if request.method == "POST":
        p = request.POST
        if p.get("action") == "reset":
            ws.theme = {**default_theme(), "updated": timezone.now().isoformat()}
            messages.success(request, "Theme reset to the standard look.")
        else:
            t = {**default_theme(), **(ws.theme or {})}
            for k in ("primary", "accent", "button_text", "page_bg", "header_bg", "header_text"):
                if re.fullmatch(r"#[0-9a-fA-F]{6}", p.get(k, "")):
                    t[k] = p[k]
            t["font"] = p.get("font") if p.get("font") in fonts else t["font"]
            t["radius"] = p.get("radius") if p.get("radius") in ("sharp", "rounded", "pill") else "rounded"
            t["field_style"] = p.get("field_style") if p.get("field_style") in ("outlined", "filled", "underline") else "outlined"
            t["show_logo"], t["show_name"] = p.get("show_logo") == "on", p.get("show_name") == "on"
            t["preset"] = p.get("preset") if p.get("preset") in THEME_PRESETS else "custom"
            t["updated"] = timezone.now().isoformat()
            ws.theme = t
            messages.success(request, "Theme saved — recipients see it on fill pages, forms and thank-you pages.")
        ws.save(update_fields=["theme"])
        return redirect("accounts:theme")
    return render(request, "accounts/ws_theme.html", {"t": {**default_theme(), **(ws.theme or {})}, "section": "theme",
                                                      "fonts": fonts, "presets": THEME_PRESETS})


@manager_required
def thank_you(request):
    from .models import THANK_YOU_ICONS

    ws = request.workspace
    if request.method == "POST":
        p = request.POST
        if p.get("action") == "reset":
            ws.thank_you = default_thank_you()
            messages.success(request, "Thank you page reset.")
        else:
            url = p.get("button_url", "").strip()
            redirect_url = p.get("redirect_url", "").strip()
            for u in (url, redirect_url):
                if u and not u.startswith(("https://", "http://", "/")):
                    messages.error(request, "Links must start with https:// (or / for a page on this site).")
                    return redirect("accounts:thank_you")
            try:
                secs = max(0, min(120, int(p.get("redirect_seconds") or 0)))
            except ValueError:
                secs = 0
            ws.thank_you = {
                "title": p.get("title", "").strip()[:120] or "Thank you!", "body": p.get("body", "")[:2000],
                "button_label": p.get("button_label", "").strip()[:60], "button_url": url[:500],
                "icon": p.get("icon") if p.get("icon") in THANK_YOU_ICONS else "🎉",
                "show_reference": p.get("show_reference") == "on", "next_steps": p.get("next_steps", "")[:2000],
                "contact": p.get("contact", "").strip()[:300], "redirect_url": redirect_url[:500], "redirect_seconds": secs,
                "confetti": p.get("confetti") == "on", "updated": timezone.now().isoformat()}
            messages.success(request, "Thank you page saved.")
        ws.save(update_fields=["thank_you"])
        return redirect("accounts:thank_you")
    return render(request, "accounts/ws_thank_you.html", {"ty": {**default_thank_you(), **(ws.thank_you or {})},
                                                          "section": "thankyou", "icons": THANK_YOU_ICONS})


def _email_sample(request, ws):
    return {"user_name": request.user.get_full_name() or "Alex Recipient", "workflow_name": "Partial withdrawal",
            "document_name": "Jordan Sample – POL-200102", "link": engine.absolute("/f/sample-link/"),
            "workspace_name": ws.name, "actor_name": engine.actor_label(request.user), "message": "Please review and sign.",
            "reason": "Missing ID copy", "step_name": "Branch review", "role": "Creator", "subject": "A message from the team",
            "fields": {}}


EMAIL_COLORS = ("header_color", "header_text", "button_color", "button_text")


@manager_required
def email_templates(request):
    ws = request.workspace
    key = request.GET.get("key") or request.POST.get("key") or "invite"
    if key not in mailer.DEFAULT_TEMPLATES:
        key = "invite"
    back = redirect(reverse("accounts:email_templates") + f"?key={key}")
    if request.method == "POST":
        action = request.POST.get("action", "save")
        p = request.POST
        if action == "preview":  # live preview while typing (JSON)
            from django.http import JsonResponse

            ctx = _email_sample(request, ws)
            subject = mailer.render(p.get("subject", ""), ctx)
            text = mailer.render(p.get("body", ""), ctx)
            return JsonResponse({"subject": subject, "html": mailer.render_html(ws, subject, text, ctx, p.get("button_label", ""))})
        if action == "reset":
            EmailTemplate.objects.filter(workspace=ws, key=key).delete()
            messages.success(request, "Template reset to default.")
        elif action == "design":
            d = dict(mailer.design(ws))
            for k in EMAIL_COLORS:
                v = p.get(k, "")
                if re.fullmatch(r"#[0-9a-fA-F]{6}", v):
                    d[k] = v
            d.update(from_name=p.get("from_name", "").strip()[:80], reply_to=p.get("reply_to", "").strip()[:200],
                     footer=p.get("footer", "")[:1000], button_label=p.get("default_button", "").strip()[:60] or "Open",
                     html=p.get("html") == "on", show_logo=p.get("show_logo") == "on", security_note=p.get("security_note") == "on")
            if d["reply_to"] and "@" not in d["reply_to"]:
                messages.error(request, "Reply-to must be an email address.")
                return back
            changed = {k: v for k, v in d.items() if mailer.DEFAULT_DESIGN.get(k) != v}  # keep only what differs from the defaults
            settings_ = {k: v for k, v in (ws.settings or {}).items() if k != "email_design"}
            if changed:
                settings_["email_design"] = changed
            ws.settings = settings_
            ws.save(update_fields=["settings"])
            messages.success(request, "Email design saved — it applies to every email.")
        elif action == "test":
            ok = mailer.send(ws, request.user.email, key, _email_sample(request, ws),
                             subject=None, body=None)
            messages.success(request, f"Test email sent to {request.user.email}.") if ok else messages.error(
                request, "This email is switched off or couldn't be delivered — check the Email log.")
        elif action == "toggle":
            t = mailer.template_obj(ws, key)
            EmailTemplate.objects.update_or_create(workspace=ws, key=key, defaults={
                "subject": t["subject"], "body": t["body"], "enabled": not t["enabled"]})
            messages.success(request, "Email switched " + ("off — it won't be sent." if t["enabled"] else "on."))
        else:
            bad = [a for a in re.split(r"[,;\s]+", (p.get("cc", "") + " " + p.get("bcc", "")).strip()) if a and "@" not in a]
            if bad:
                messages.error(request, f"Not an email address: {bad[0]}")
                return back
            EmailTemplate.objects.update_or_create(workspace=ws, key=key, defaults={
                "subject": p.get("subject", "")[:200], "body": p.get("body", ""), "enabled": p.get("enabled") == "on",
                "cc": p.get("cc", "").strip()[:500], "bcc": p.get("bcc", "").strip()[:500],
                "button_label": p.get("button_label", "").strip()[:60]})
            messages.success(request, "Email template saved.")
        return back
    t = mailer.template_obj(ws, key)
    custom = {e.key: e for e in EmailTemplate.objects.filter(workspace=ws)}
    grouped = {}
    for k, (group, _, _) in mailer.DEFAULT_TEMPLATES.items():
        e = custom.get(k)
        grouped.setdefault(group, []).append({"key": k, "label": k.replace("_", " ").capitalize(),
                                              "off": bool(e and not e.enabled), "custom": bool(e)})
    ctx = _email_sample(request, ws)
    subject = mailer.render(t["subject"], ctx)
    return render(request, "accounts/ws_email_templates.html", {
        "key": key, "t": t, "grouped": grouped, "section": "emails", "d": mailer.design(ws),
        "label": key.replace("_", " ").capitalize(), "description": mailer.DESCRIPTIONS.get(key, ""),
        "placeholders": [("{" + ph + "}", ph.replace("_", " ").capitalize()) for ph in mailer.PLACEHOLDERS.get(key, []) + mailer.COMMON],
        "preview_subject": subject, "preview_html": mailer.render_html(ws, subject, mailer.render(t["body"], ctx), ctx, t["button_label"])})


@manager_required
def domains(request):
    return render(request, "accounts/ws_domains.html", {"section": "domains"})


def _usage_range(request):
    ws = request.workspace
    end = parse_date(request.GET.get("to") or "") or timezone.localdate()
    start = parse_date(request.GET.get("from") or "") or min(timezone.localtime(ws.created_at).date(), end - timezone.timedelta(days=30))
    return start, end


@manager_required
def subscription(request):
    ws = request.workspace
    since = (ws.plan_expires - timezone.timedelta(days=365)) if ws.plan_expires else timezone.localdate() - timezone.timedelta(days=365)
    qs = UsageEvent.objects.filter(workspace=ws, created_at__date__gte=since)
    sends = qs.filter(kind=UsageEvent.SEND).aggregate(n=Sum("credits"))["n"] or 0
    bots = qs.filter(kind=UsageEvent.BOT).aggregate(n=Sum("credits"))["n"] or 0
    used = sends + bots
    return render(request, "accounts/ws_subscription.html", {
        "section": "subscription", "sends": sends, "bots": bots, "used": used, "since": since,
        "pct": min(100, int(used * 100 / ws.credits_limit)) if ws.credits_limit else 0})


@manager_required
def billing(request):
    ws = request.workspace
    if request.method == "POST":
        ws.email = request.POST.get("billing_email", ws.email)
        ws.save(update_fields=["email"])
        messages.success(request, "Billing contact saved.")
        return redirect("accounts:billing")
    months = (UsageEvent.objects.filter(workspace=ws).annotate(m=TruncMonth("created_at"))
              .values("m").annotate(credits=Sum("credits")).order_by("-m")[:12])
    return render(request, "accounts/ws_billing.html", {"section": "billing", "months": months})


@manager_required
def usage_log(request):
    ws = request.workspace
    start, end = _usage_range(request)
    qs = UsageEvent.objects.filter(workspace=ws, created_at__date__gte=start, created_at__date__lte=end)
    rows = (qs.values("workflow_id", "workflow_name")
            .annotate(sends=Count("id", filter=Q(kind=UsageEvent.SEND)), bots=Count("id", filter=Q(kind=UsageEvent.BOT)),
                      credits=Sum("credits")).order_by("-credits"))
    total = {"sends": sum(r["sends"] for r in rows), "bots": sum(r["bots"] for r in rows), "credits": sum(r["credits"] or 0 for r in rows)}
    if request.GET.get("format") == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Name", "Sends", "Bot actions", "Credits"])
        for r in rows:
            w.writerow([r["workflow_name"] or "(deleted)", r["sends"], r["bots"], r["credits"]])
        w.writerow(["Total", total["sends"], total["bots"], total["credits"]])
        resp = HttpResponse(buf.getvalue(), content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="usage-{start}-{end}.csv"'
        return resp
    return render(request, "accounts/ws_usage.html", {"rows": rows, "total": total, "start": start, "end": end, "section": "usage"})


@manager_required
def email_log(request):
    emails = (OutboundEmail.objects.filter(workspace=request.workspace).select_related("packet")
              .defer("packet__definition", "packet__data")[:300])
    return render(request, "accounts/ws_email_log.html", {"emails": emails, "section": "email_log"})


@login_required
def datasheets(request, pk=None):
    ws = request.workspace
    if pk:
        sheet = get_object_or_404(DataSheet, workspace=ws, pk=pk)
        if request.method == "POST" and request.POST.get("delete") and request.membership.can_manage:
            sheet.delete()
            messages.success(request, "Spreadsheet deleted.")
            return redirect("accounts:datasheets")
        rows = sheet.rows.order_by("-created_at")
        if request.GET.get("format") == "csv":
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=sheet.columns, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r.values)
            resp = HttpResponse(buf.getvalue(), content_type="text/csv")
            resp["Content-Disposition"] = f'attachment; filename="{sheet.name}.csv"'
            return resp
        return render(request, "accounts/ws_datasheet.html", {"sheet": sheet, "rows": rows[:500], "section": "data"})
    return render(request, "accounts/ws_datasheets.html", {"sheets": DataSheet.objects.filter(workspace=ws).annotate(n=Count("rows")),
                                                           "section": "data"})


# ---- Team management ---------------------------------------------------------------------------
def _open_runs(ws):
    from flows.models import StepRun

    return StepRun.objects.filter(packet__workspace=ws, packet__status="in_progress", packet__trashed_at__isnull=True,
                                  status__in=[StepRun.PENDING, StepRun.OPENED])


def _people_counts(ws):
    today = timezone.localdate()
    return {"active_count": Membership.objects.filter(workspace=ws, active=True).count(),
            "blocked_count": Membership.objects.filter(workspace=ws, active=False).count(),
            "group_count": Group.objects.filter(workspace=ws).count(),
            "invite_count": Invitation.objects.filter(workspace=ws, accepted_at__isnull=True).count(),
            "ooo_count": Profile.objects.filter(user__memberships__workspace=ws, ooo_from__lte=today, ooo_until__gte=today).count()}


@login_required
def team(request):
    ws = request.workspace
    qs = Membership.objects.filter(workspace=ws).select_related("user", "user__profile").prefetch_related("groups")
    status = request.GET.get("status", "active")
    if status == "active":
        qs = qs.filter(active=True)
    elif status == "blocked":
        qs = qs.filter(active=False)
    if request.GET.get("role"):
        qs = qs.filter(role=request.GET["role"])
    if request.GET.get("group"):
        qs = qs.filter(groups__pk=request.GET["group"])
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(user__first_name__icontains=q) | Q(user__last_name__icontains=q) | Q(user__email__icontains=q)
                       | Q(position__icontains=q))
    sort = request.GET.get("sort", "name")
    qs = qs.order_by({"name": "user__first_name", "-name": "-user__first_name", "email": "user__email", "role": "role",
                      "active": "-user__last_login"}.get(sort, "user__first_name"))
    members = list(qs.distinct())
    open_by_user = dict(_open_runs(ws).filter(assignee_user__isnull=False).values_list("assignee_user").annotate(n=Count("id")))
    overdue_by_user = dict(_open_runs(ws).filter(assignee_user__isnull=False, due_at__lt=timezone.now())
                           .values_list("assignee_user").annotate(n=Count("id")))
    today = timezone.localdate()
    for m in members:
        m.open_steps = open_by_user.get(m.user_id, 0)
        m.overdue_steps = overdue_by_user.get(m.user_id, 0)
        pr = getattr(m.user, "profile", None)
        m.is_away = bool(pr and pr.ooo_from and pr.ooo_until and pr.ooo_from <= today <= pr.ooo_until)
        m.group_list = list(m.groups.all())
    return render(request, "accounts/team.html", {
        "members": members, "status": status, "roles": Membership.ROLES, "role_help": Membership.ROLE_HELP,
        "groups": Group.objects.filter(workspace=ws).annotate(n=Count("members")), "q": q, "nav": "team", "tab": "members",
        "invites": Invitation.objects.filter(workspace=ws, accepted_at__isnull=True).select_related("invited_by"), "sort": sort,
        "open_total": sum(open_by_user.values()), **_people_counts(ws)})


@login_required
def team_member_panel(request, pk):
    """Side panel: one teammate's details, open work, groups and (for managers) quick edits."""
    ws = request.workspace
    m = get_object_or_404(Membership.objects.select_related("user", "user__profile"), workspace=ws, pk=pk)
    runs = _open_runs(ws).filter(Q(assignee_user=m.user) | Q(claimed_by=m.user)).select_related("packet").order_by("due_at", "created_at")
    from flows.models import StepRun

    done_30 = StepRun.objects.filter(packet__workspace=ws, assignee_user=m.user, status=StepRun.COMPLETED,
                                     completed_at__gte=timezone.now() - timezone.timedelta(days=30)).count()
    return render(request, "accounts/partials/member_panel.html", {
        "m": m, "runs": runs[:8], "open_count": runs.count(), "done_30": done_30, "roles": Membership.ROLES,
        "role_help": Membership.ROLE_HELP, "groups": Group.objects.filter(workspace=ws), "member_groups": set(m.groups.values_list("pk", flat=True)),
        "profile": getattr(m.user, "profile", None), "today": timezone.localdate()})


@manager_required
@require_POST
def team_invite(request):
    ws = request.workspace
    role = request.POST.get("role", Membership.CREATOR)
    if role == Membership.OWNER and request.membership.role != Membership.OWNER:
        raise PermissionDenied
    sent = 0
    for raw in request.POST.get("emails", "").replace(";", ",").replace("\n", ",").split(","):
        email = raw.strip().lower()
        if "@" not in email:
            continue
        existing = User.objects.filter(email__iexact=email).first()
        if existing and Membership.objects.filter(user=existing, workspace=ws).exists():
            continue
        inv = Invitation.objects.create(workspace=ws, email=email, role=role, invited_by=request.user)
        mailer.send(ws, email, "teammate_invite", {"actor_name": engine.actor_label(request.user), "role": inv.get_role_display(),
                                                   "link": engine.absolute(reverse("accept_invite", args=[inv.token]))})
        if existing:
            engine.notify(existing, ws, f"You're invited to join {ws.name}", "", reverse("accept_invite", args=[inv.token]), kind="team")
        sent += 1
    messages.success(request, f"{sent} invitation(s) sent.")
    return redirect("accounts:team")


@manager_required
@require_POST
def team_member(request, pk, action):
    ws = request.workspace
    m = get_object_or_404(Membership, workspace=ws, pk=pk)
    if m.role == Membership.OWNER and request.membership.role != Membership.OWNER:
        raise PermissionDenied("Only an owner can change another owner.")
    if action == "role":
        new = request.POST.get("role")
        if new not in dict(Membership.ROLES):
            messages.error(request, "Choose a valid role.")
            return redirect("accounts:team")
        if new == Membership.OWNER and request.membership.role != Membership.OWNER:
            raise PermissionDenied
        if m.role == Membership.OWNER and new != Membership.OWNER and not ws.memberships.filter(role=Membership.OWNER).exclude(pk=m.pk).exists():
            messages.error(request, "A workspace needs at least one owner.")
            return redirect("accounts:team")
        m.role = new
        m.save(update_fields=["role"])
    elif action in ("block", "activate"):
        if m.user_id == request.user.id:
            messages.error(request, "You can't block yourself.")
            return redirect("accounts:team")
        m.active = action == "activate"
        m.save(update_fields=["active"])
        if ws.setting("teammate_emails"):
            mailer.send(ws, m.user.email, "custom", {"subject": f"Your access to {ws.name} was {'activated' if m.active else 'blocked'}",
                                                     "message": f"Your access to the {ws.name} workspace was {'activated' if m.active else 'blocked'}."})
    elif action == "remove":
        if m.user_id == request.user.id:
            messages.error(request, "Use “Leave workspace” instead.")
            return redirect("accounts:team")
        m.delete()
    elif action == "groups":
        m.groups.set(Group.objects.filter(workspace=ws, pk__in=request.POST.getlist("groups")))
    elif action == "position":
        m.position = request.POST.get("position", "").strip()[:120]
        m.save(update_fields=["position"])
    engine.audit(ws, f"team.{action}", f"{m.user.email}", actor=request.user, request=request)
    messages.success(request, "Teammate updated.")
    return safe_next(request, "accounts:team")


@manager_required
@require_POST
def team_bulk_group(request):
    ws = request.workspace
    group = get_object_or_404(Group, workspace=ws, pk=request.POST.get("group"))
    group.members.add(*Membership.objects.filter(workspace=ws, pk__in=request.POST.getlist("ids")))
    messages.success(request, f"Added to {group.name}.")
    return redirect("accounts:team")


@manager_required
@require_POST
def invite_cancel(request, pk):
    Invitation.objects.filter(workspace=request.workspace, pk=pk, accepted_at__isnull=True).delete()
    messages.success(request, "Invitation cancelled.")
    return redirect("accounts:team")


def _group_usage(ws):
    """{group id: {"steps": [(workflow name, step name, workflow id)], "queue": open steps waiting for the group}}."""
    from flows.models import Step

    usage = {}
    for gid, wname, sname, wid in Step.objects.filter(workflow__workspace=ws, workflow__trashed_at__isnull=True,
                                                      recipient_group__isnull=False).values_list(
            "recipient_group", "workflow__name", "name", "workflow_id"):
        usage.setdefault(gid, {"steps": [], "queue": 0})["steps"].append((wname, sname, wid))
    for gid, n in _open_runs(ws).filter(assignee_group__isnull=False, claimed_by__isnull=True).values_list(
            "assignee_group").annotate(n=Count("id")):
        usage.setdefault(gid, {"steps": [], "queue": 0})["queue"] = n
    return usage


@login_required
def groups(request):
    ws = request.workspace
    if request.method == "POST":
        if not request.membership.can_manage:
            raise PermissionDenied("Only workspace owners and supervisors can do this.")
        if request.POST.get("delete"):
            g = Group.objects.filter(workspace=ws, pk=request.POST["delete"]).first()
            if g:
                engine.audit(ws, "team.group_deleted", g.name, actor=request.user, request=request)
                g.delete()
                messages.success(request, "Group deleted.")
        else:
            gid = request.POST.get("id")
            g = get_object_or_404(Group, workspace=ws, pk=gid) if gid else Group(workspace=ws)
            name = request.POST.get("name", "").strip()[:120]
            if not name:
                messages.error(request, "Give the group a name.")
                return redirect("accounts:groups")
            if Group.objects.filter(workspace=ws, name__iexact=name).exclude(pk=g.pk).exists():
                messages.error(request, f"A group called “{name}” already exists.")
                return redirect("accounts:groups")
            g.name = name
            g.description = request.POST.get("description", "")[:255]
            g.save()
            g.members.set(Membership.objects.filter(workspace=ws, pk__in=request.POST.getlist("members")))
            engine.audit(ws, "team.group_saved", g.name, actor=request.user, request=request)
            messages.success(request, f"“{g.name}” saved.")
        return redirect("accounts:groups")
    qs = Group.objects.filter(workspace=ws).prefetch_related("members__user__profile")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(description__icontains=q) | Q(members__user__first_name__icontains=q)
                       | Q(members__user__email__icontains=q)).distinct()
    usage = _group_usage(ws)
    rows = []
    show = request.GET.get("show", "")
    for g in qs:
        u = usage.get(g.pk, {"steps": [], "queue": 0})
        mem = [m for m in g.members.all()]
        g.member_list, g.active_members = mem, [m for m in mem if m.active]
        g.steps, g.queue = u["steps"], u["queue"]
        g.warn = bool(g.steps) and not g.active_members
        if show == "unused" and g.steps:
            continue
        if show == "attention" and not g.warn:
            continue
        rows.append(g)
    return render(request, "accounts/groups.html", {
        "groups": rows, "q": q, "show": show, "nav": "team", "tab": "groups",
        "attention": sum(1 for g in Group.objects.filter(workspace=ws) if usage.get(g.pk, {}).get("steps") and not g.members.filter(active=True).exists()),
        "roles": Membership.ROLES, "role_help": Membership.ROLE_HELP, **_people_counts(ws)})


@manager_required
def group_panel(request, pk=None):
    ws = request.workspace
    g = get_object_or_404(Group, workspace=ws, pk=pk) if pk else None
    usage = _group_usage(ws).get(g.pk, {"steps": [], "queue": 0}) if g else {"steps": [], "queue": 0}
    return render(request, "accounts/partials/group_panel.html", {
        "g": g, "usage": usage, "chosen": set(g.members.values_list("pk", flat=True)) if g else set(),
        "members": Membership.objects.filter(workspace=ws, active=True).select_related("user", "user__profile").order_by("user__first_name")})


# ---- Contacts -----------------------------------------------------------------------------------
def _contact_activity(ws, emails):
    """{lowercased email: (documents sent, last sent)} from step invitations."""
    from django.db.models import Max
    from django.db.models.functions import Lower

    from flows.models import StepRun

    rows = (StepRun.objects.filter(packet__workspace=ws).annotate(e=Lower("assignee_email")).filter(e__in=[x.lower() for x in emails])
            .values("e").annotate(n=Count("packet", distinct=True), last=Max("created_at")))
    return {r["e"]: (r["n"], r["last"]) for r in rows}


@login_required
def contacts(request):
    ws = request.workspace
    base = Contact.objects.filter(workspace=ws)
    qs = base
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(email__icontains=q) | Q(company__icontains=q) | Q(phone__icontains=q) | Q(notes__icontains=q))
    company = request.GET.get("company", "")
    if company:
        qs = qs.filter(company=company)
    sort = request.GET.get("sort", "name")
    qs = qs.order_by({"name": "name", "-name": "-name", "email": "email", "company": "company", "created": "-created_at"}.get(sort, "name"))
    rows = list(qs[:500])
    act = _contact_activity(ws, [c.email for c in rows])
    for c in rows:
        c.docs, c.last_sent = act.get(c.email.lower(), (0, None))
    if sort == "active":
        rows.sort(key=lambda c: (c.last_sent is None, -(c.last_sent.timestamp() if c.last_sent else 0)))
    companies = list(base.exclude(company="").values_list("company", flat=True).distinct().order_by("company"))
    week = timezone.now() - timezone.timedelta(days=30)
    return render(request, "accounts/contacts.html", {
        "contacts": rows, "q": q, "sort": sort, "company": company, "companies": companies, "total": base.count(),
        "new_30": base.filter(created_at__gte=week).count(), "with_docs": sum(1 for c in rows if c.docs),
        "form": ContactForm(), "nav": "contacts"})


@login_required
def contact_panel(request, pk):
    from flows.models import StepRun

    ws = request.workspace
    c = get_object_or_404(Contact, workspace=ws, pk=pk)
    runs = (StepRun.objects.filter(packet__workspace=ws, assignee_email__iexact=c.email).select_related("packet", "packet__workflow")
            .order_by("-created_at"))
    return render(request, "accounts/partials/contact_panel.html", {"c": c, "runs": runs[:10], "doc_count": runs.values("packet").distinct().count(),
                                                                    "open_count": runs.filter(status__in=["pending", "opened"]).count()})


@login_required
def contact_edit(request, pk=None):
    if not request.membership.can_create:
        messages.error(request, "Your role can view contacts but not change them.")
        return redirect("accounts:contacts")
    ws = request.workspace
    c = get_object_or_404(Contact, workspace=ws, pk=pk) if pk else None
    form = ContactForm(request.POST or None, instance=c)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.workspace = ws
        obj.save()
        messages.success(request, "Contact saved.")
        return redirect("accounts:contacts")
    return render(request, "accounts/partials/contact_form.html", {"form": form, "contact": c})


@login_required
@require_POST
def contact_delete(request):
    if not request.membership.can_create:
        messages.error(request, "Your role can view contacts but not change them.")
        return redirect("accounts:contacts")
    ids = request.POST.getlist("ids") or [request.POST.get("id")]
    n, _ = Contact.objects.filter(workspace=request.workspace, pk__in=[i for i in ids if i]).delete()
    messages.success(request, f"{n} contact(s) deleted.")
    return redirect("accounts:contacts")


@login_required
def contacts_import(request):
    if not request.membership.can_create:
        messages.error(request, "Your role can view contacts but not change them.")
        return redirect("accounts:contacts")
    if request.method == "POST":
        upload = request.FILES.get("file")
        text = upload.read().decode("utf-8-sig") if upload else request.POST.get("text", "")
        created = 0
        for row in csv.DictReader(io.StringIO(text)):
            low = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            email = low.get("email") or low.get("e-mail")
            if not email:
                continue
            name = low.get("name") or " ".join(x for x in [low.get("first name"), low.get("last name")] if x) or email
            Contact.objects.update_or_create(workspace=request.workspace, email=email, defaults={
                "name": name, "phone": low.get("phone", ""), "company": low.get("company", "")})
            created += 1
        messages.success(request, f"{created} contact(s) imported.")
        return redirect("accounts:contacts")
    return render(request, "accounts/partials/contacts_import.html")


@login_required
def contacts_export(request):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "email", "phone", "company", "notes"])
    for c in Contact.objects.filter(workspace=request.workspace):
        w.writerow([c.name, c.email, c.phone, c.company, c.notes])
    resp = HttpResponse(buf.getvalue(), content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="contacts.csv"'
    return resp
