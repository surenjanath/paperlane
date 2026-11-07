"""Super admin console (/platform/): every workspace and user on this Paperlane installation, platform settings,
feature switches, background jobs, the platform audit log and system health. Django superusers only."""
import datetime as dt
import functools
import os
import platform as py_platform
import sys

import django
from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import PasswordResetForm
from django.contrib.auth.views import redirect_to_login
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import connection
from django.db.models import Count, Max, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from accounts.models import ApiToken, Invitation, Membership, Profile, Workspace

from .models import FEATURES, Broadcast, PlatformEvent, PlatformSettings, SignInEvent, get_settings, log

User = get_user_model()


def superuser_required(view):
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not request.user.is_superuser or not request.user.is_active:
            raise PermissionDenied("Super admins only.")
        return view(request, *args, **kwargs)
    return wrapper


def _ctx(section, **extra):
    return {"section": section, "nav": "platform", **extra}


# ---- overview ------------------------------------------------------------------------------------------------------
def _dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _files_size(ws):
    from flows.models import PacketFile

    total = 0
    for f in PacketFile.objects.filter(packet__workspace=ws).only("file").iterator():
        try:
            total += f.file.size
        except (OSError, ValueError):
            pass
    return total


def health_checks():
    """[{label, state: ok|warn|bad, detail, link}] for the overview and the System page."""
    from flows import ai_providers
    from flows.models import BotJob, BotLog, WebhookDelivery

    now = timezone.now()
    ps = get_settings()
    out = []

    def add(label, state, detail, link=""):
        out.append({"label": label, "state": state, "detail": detail, "link": link})

    beat = cache.get("scheduler-heartbeat")
    if beat and now - beat < dt.timedelta(minutes=5):
        add("Background jobs", "ok", f"Scheduler ran {int((now - beat).total_seconds())} s ago.")
    else:
        add("Background jobs", "bad", "The scheduler hasn't run in the last 5 minutes — reminders, deadlines, delayed bots and "
            "webhook retries are paused. Leave PAPERLANE_SCHEDULER on or run manage.py paperlane_tick --loop 30.",
            reverse("superadmin:jobs"))
    late = BotJob.objects.filter(done=False, run_at__lt=now - dt.timedelta(minutes=10)).count()
    if late:
        add("Delayed bots", "warn", f"{late} delayed bot(s) are more than 10 minutes late.", reverse("superadmin:jobs"))
    failed_bots = BotLog.objects.filter(status="failed", created_at__gte=now - dt.timedelta(days=1)).count()
    add("Bot failures (24 h)", "ok" if not failed_bots else "warn", f"{failed_bots} failed bot run(s).", reverse("superadmin:jobs"))
    failed_hooks = WebhookDelivery.objects.filter(status=WebhookDelivery.FAILED, created_at__gte=now - dt.timedelta(days=1)).count()
    add("Webhook deliveries (24 h)", "ok" if not failed_hooks else "warn", f"{failed_hooks} delivery(ies) gave up.",
        reverse("superadmin:jobs"))
    backend = settings.EMAIL_BACKEND.rsplit(".", 2)[-2]
    if backend in ("console", "locmem", "dummy", "filebased"):
        add("Email delivery", "warn", f"EMAIL_BACKEND is “{backend}” — emails are logged but not delivered to inboxes.")
    else:
        add("Email delivery", "ok", f"Sending with the {backend} backend from {settings.DEFAULT_FROM_EMAIL}.")
    site = getattr(settings, "SITE_URL", "")
    local = any(h in site for h in ("127.0.0.1", "localhost", "[::1]"))
    add("Public address", "warn" if local or not site else "ok",
        f"Links in emails use {site} — people on other computers can't open them. Set PAPERLANE_SITE_URL." if local or not site
        else f"Links in emails use {site}.")
    if settings.DEBUG:
        add("Debug mode", "warn", "DEBUG is on. Turn it off (PAPERLANE_DEBUG=0) before exposing Paperlane to the internet.")
    else:
        add("Debug mode", "ok", "DEBUG is off.")
    if str(settings.SECRET_KEY).startswith("django-insecure") or len(str(settings.SECRET_KEY)) < 40:
        add("Secret key", "bad", "SECRET_KEY is a development key. Set PAPERLANE_SECRET_KEY to a long random value.")
    else:
        add("Secret key", "ok", "A custom secret key is configured.")
    engines = [e for e in ai_providers.engines() if e["id"] != ai_providers.PLANNER]
    if not ps.feature("ai"):
        add("AI models", "ok", "AI is switched off platform-wide.")
    else:
        add("AI models", "ok" if engines else "warn", f"{len(engines)} model(s) available ({', '.join(sorted({e['group'] for e in engines}))})."
            if engines else "No AI model is reachable — AI bots and Ask AI will fail. Start Ollama or set an API key.")
    supers = User.objects.filter(is_superuser=True, is_active=True)
    no2fa = [u.email for u in supers if not getattr(Profile.objects.filter(user=u).first(), "two_factor_on", False)]
    add("Super admin accounts", "ok" if not no2fa else "warn",
        f"{supers.count()} super admin(s)" + (f"; without two-step verification: {', '.join(no2fa)}." if no2fa else ", all with two-step verification."),
        reverse("superadmin:users") + "?filter=super")
    if ps.maintenance_mode:
        add("Maintenance mode", "warn", "Maintenance mode is ON — only super admins can use the app.", reverse("superadmin:settings") + "#operations")
    return out


@superuser_required
def overview(request):
    from flows.models import Packet, StepRun, Workflow

    now = timezone.now()
    since = now - dt.timedelta(days=13)
    daily = {(since + dt.timedelta(days=i)).date(): 0 for i in range(14)}
    for d in Packet.objects.filter(created_at__gte=since.replace(hour=0, minute=0, second=0)).values_list("created_at", flat=True):
        day = timezone.localtime(d).date()
        if day in daily:
            daily[day] += 1
    peak = max(daily.values()) or 1
    stats = {
        "workspaces": Workspace.objects.count(),
        "suspended": Workspace.objects.filter(suspended_at__isnull=False).count(),
        "users": User.objects.filter(is_active=True).count(),
        "new_users": User.objects.filter(date_joined__gte=now - dt.timedelta(days=30)).count(),
        "active_users": User.objects.filter(last_login__gte=now - dt.timedelta(days=7)).count(),
        "workflows": Workflow.objects.filter(trashed_at__isnull=True, status=Workflow.PUBLISHED).count(),
        "in_progress": Packet.objects.filter(status=Packet.IN_PROGRESS, trashed_at__isnull=True).count(),
        "completed_30": Packet.objects.filter(status=Packet.COMPLETED, completed_at__gte=now - dt.timedelta(days=30)).count(),
        "open_steps": StepRun.objects.filter(status__in=[StepRun.PENDING, StepRun.OPENED], packet__status=Packet.IN_PROGRESS).count(),
        "overdue": StepRun.objects.filter(status__in=[StepRun.PENDING, StepRun.OPENED], due_at__lt=now,
                                          packet__status=Packet.IN_PROGRESS).count(),
    }
    chart = [{"day": d, "n": n, "pct": round(n * 100 / peak)} for d, n in daily.items()]
    top = (Workspace.objects.annotate(docs=Count("packets", filter=Q(packets__created_at__gte=now - dt.timedelta(days=30))),
                                      members=Count("memberships", filter=Q(memberships__active=True), distinct=True))
           .order_by("-docs")[:5])
    return render(request, "superadmin/overview.html", _ctx(
        "overview", stats=stats, chart=chart, checks=health_checks(), top=top,
        events=PlatformEvent.objects.select_related("actor")[:8]))


# ---- workspaces ------------------------------------------------------------------------------------------------------
class WorkspaceCreate(forms.Form):
    name = forms.CharField(max_length=160)
    owner_email = forms.EmailField(help_text="An existing user becomes the owner; anyone else gets an invitation.")
    plan = forms.CharField(max_length=40, required=False)


@superuser_required
def workspaces(request):
    ps = get_settings()
    form = WorkspaceCreate(request.POST or None, initial={"plan": ps.default_plan})
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        ws = Workspace.objects.create(name=d["name"], plan=d["plan"] or ps.default_plan, credits_limit=ps.default_credits,
                                      plan_expires=(timezone.now() + dt.timedelta(days=ps.default_plan_days)).date(),
                                      email=d["owner_email"])
        owner = User.objects.filter(email__iexact=d["owner_email"]).first()
        if owner:
            Membership.objects.create(user=owner, workspace=ws, role=Membership.OWNER, position="Administrator")
            note = f"{owner.email} is the owner."
        else:
            inv = Invitation.objects.create(workspace=ws, email=d["owner_email"], role=Membership.OWNER, invited_by=request.user)
            _send_invite(ws, inv, request.user)
            note = f"An invitation was sent to {inv.email}."
        log(request, "workspace.created", ws.name, owner=d["owner_email"])
        messages.success(request, f"Workspace “{ws.name}” created. {note}")
        return redirect("superadmin:workspace", ws.pk)
    q = request.GET.get("q", "").strip()
    flt = request.GET.get("filter", "")
    qs = Workspace.objects.annotate(
        members=Count("memberships", filter=Q(memberships__active=True), distinct=True),
        flows=Count("workflows", filter=Q(workflows__trashed_at__isnull=True), distinct=True),
        docs=Count("packets", distinct=True), last_doc=Max("packets__created_at")).order_by("name")
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(slug__icontains=q) | Q(email__icontains=q))
    if flt == "suspended":
        qs = qs.filter(suspended_at__isnull=False)
    elif flt == "expired":
        qs = qs.filter(plan_expires__lt=timezone.localdate())
    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    return render(request, "superadmin/workspaces.html", _ctx("workspaces", page=page, q=q, flt=flt, form=form, today=timezone.localdate()))


def _send_invite(ws, inv, actor):
    from flows import engine, mailer

    mailer.send(ws, inv.email, "teammate_invite", {"actor_name": engine.actor_label(actor), "role": inv.get_role_display(),
                                                   "link": engine.absolute(reverse("accept_invite", args=[inv.token]))})


@superuser_required
def workspace_detail(request, pk):
    from flows.models import Packet, Workflow

    ws = get_object_or_404(Workspace, pk=pk)
    if request.method == "POST":
        return _workspace_action(request, ws)
    members = ws.memberships.select_related("user", "user__profile").order_by("-active", "role", "user__email")
    now = timezone.now()
    usage = {
        "workflows": Workflow.objects.filter(workspace=ws, trashed_at__isnull=True).count(),
        "published": Workflow.objects.filter(workspace=ws, trashed_at__isnull=True, status=Workflow.PUBLISHED).count(),
        "documents": Packet.objects.filter(workspace=ws).count(),
        "in_progress": Packet.objects.filter(workspace=ws, status=Packet.IN_PROGRESS, trashed_at__isnull=True).count(),
        "docs_30": Packet.objects.filter(workspace=ws, created_at__gte=now - dt.timedelta(days=30)).count(),
        "storage": _files_size(ws),
        "tokens": ApiToken.objects.filter(workspace=ws, revoked_at__isnull=True).count(),
    }
    ps = get_settings()
    feats = [{"key": k, "label": l, "help": h, "platform": ps.feature(k), "override": (ws.feature_overrides or {}).get(k)}
             for k, l, h in FEATURES if k != "create_workspaces"]
    return render(request, "superadmin/workspace_detail.html", _ctx(
        "workspaces", ws=ws, members=members, usage=usage, feats=feats, roles=Membership.ROLES,
        invites=ws.invitations.filter(accepted_at__isnull=True),
        events=PlatformEvent.objects.filter(target=ws.name)[:10],
        expired=bool(ws.plan_expires and ws.plan_expires < timezone.localdate())))


def _workspace_action(request, ws):
    p = request.POST
    op = p.get("op")
    back = redirect("superadmin:workspace", ws.pk)
    if op == "plan":
        ws.plan = p.get("plan", ws.plan).strip()[:40] or ws.plan
        try:
            ws.plan_expires = dt.date.fromisoformat(p["plan_expires"]) if p.get("plan_expires") else None
            ws.credits_limit = max(0, int(p.get("credits_limit") or ws.credits_limit))
        except ValueError:
            messages.error(request, "Check the expiry date and the credits.")
            return back
        ws.admin_notes = p.get("admin_notes", "")[:5000]
        ws.save(update_fields=["plan", "plan_expires", "credits_limit", "admin_notes"])
        log(request, "workspace.plan", ws.name, plan=ws.plan, expires=str(ws.plan_expires), credits=ws.credits_limit)
        messages.success(request, "Plan saved.")
    elif op == "features":
        overrides = {}
        for k, _, _ in FEATURES:
            v = p.get(f"feat_{k}", "inherit")
            if v in ("on", "off"):
                overrides[k] = v == "on"
        ws.feature_overrides = overrides
        ws.save(update_fields=["feature_overrides"])
        log(request, "workspace.features", ws.name, **{k: v for k, v in overrides.items()})
        messages.success(request, "Feature switches saved.")
    elif op == "suspend":
        ws.suspended_at = timezone.now()
        ws.suspended_reason = p.get("reason", "").strip()[:300]
        ws.save(update_fields=["suspended_at", "suspended_reason"])
        log(request, "workspace.suspended", ws.name, reason=ws.suspended_reason)
        messages.success(request, f"{ws.name} is suspended. Its members can't open it until you restore it.")
    elif op == "restore":
        ws.suspended_at, ws.suspended_reason = None, ""
        ws.save(update_fields=["suspended_at", "suspended_reason"])
        log(request, "workspace.restored", ws.name)
        messages.success(request, f"{ws.name} is active again.")
    elif op == "member_role":
        m = get_object_or_404(Membership, pk=p.get("member"), workspace=ws)
        role = p.get("role")
        if role not in dict(Membership.ROLES):
            return back
        if m.role == Membership.OWNER and role != Membership.OWNER and \
                not ws.memberships.filter(role=Membership.OWNER, active=True).exclude(pk=m.pk).exists():
            messages.error(request, "A workspace needs at least one active owner.")
            return back
        m.role = role
        m.save(update_fields=["role"])
        log(request, "workspace.member_role", ws.name, user=m.user.email, role=role)
        messages.success(request, f"{m.user.email} is now {m.get_role_display()}.")
    elif op in ("member_deactivate", "member_activate"):
        m = get_object_or_404(Membership, pk=p.get("member"), workspace=ws)
        if op == "member_deactivate" and m.role == Membership.OWNER and \
                not ws.memberships.filter(role=Membership.OWNER, active=True).exclude(pk=m.pk).exists():
            messages.error(request, "A workspace needs at least one active owner.")
            return back
        m.active = op == "member_activate"
        m.save(update_fields=["active"])
        log(request, f"workspace.{op}", ws.name, user=m.user.email)
        messages.success(request, f"{m.user.email} {'can' if m.active else 'can no longer'} use {ws.name}.")
    elif op == "member_add":
        email = p.get("email", "").strip().lower()
        role = p.get("role") if p.get("role") in dict(Membership.ROLES) else Membership.CREATOR
        user = User.objects.filter(email__iexact=email).first()
        if not email or "@" not in email:
            messages.error(request, "Enter an email address.")
        elif user:
            m, created = Membership.objects.get_or_create(user=user, workspace=ws, defaults={"role": role})
            if not created:
                m.role, m.active = role, True
                m.save(update_fields=["role", "active"])
            log(request, "workspace.member_added", ws.name, user=email, role=role)
            messages.success(request, f"{email} added as {m.get_role_display()}.")
        else:
            inv = Invitation.objects.create(workspace=ws, email=email, role=role, invited_by=request.user)
            _send_invite(ws, inv, request.user)
            log(request, "workspace.invited", ws.name, user=email, role=role)
            messages.success(request, f"No account for {email} yet — an invitation was sent.")
    elif op == "join":
        m, _ = Membership.objects.get_or_create(user=request.user, workspace=ws, defaults={"role": Membership.OWNER})
        if not m.active:
            m.active = True
            m.save(update_fields=["active"])
        request.user.profile.current_workspace = ws
        request.user.profile.save(update_fields=["current_workspace"])
        log(request, "workspace.joined_by_super_admin", ws.name, role=m.role)
        messages.success(request, f"You joined {ws.name} as {m.get_role_display()} and switched to it.")
        return redirect("core:dashboard")
    elif op == "delete":
        if p.get("confirm", "").strip() != ws.slug:
            messages.error(request, f"Type the workspace ID “{ws.slug}” to delete it.")
            return back
        if request.user.profile.current_workspace_id == ws.pk:
            Profile.objects.filter(pk=request.user.profile.pk).update(current_workspace=None)
        name = ws.name
        ws.delete()
        log(request, "workspace.deleted", name)
        messages.success(request, f"Workspace “{name}” and all its workflows and documents were deleted.")
        return redirect("superadmin:workspaces")
    return back


# ---- users -----------------------------------------------------------------------------------------------------------
class UserCreate(forms.Form):
    email = forms.EmailField()
    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    workspace = forms.ModelChoiceField(Workspace.objects.order_by("name"), required=False, empty_label="— none —")
    role = forms.ChoiceField(choices=Membership.ROLES, initial=Membership.CREATOR)
    superuser = forms.BooleanField(required=False, label="Super admin")

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("A user with this email already exists.")
        return email


class _AdminResetForm(PasswordResetForm):
    """Also reaches accounts created by an admin that have no password yet (Django skips those)."""

    def __init__(self, *args, target=None, **kwargs):
        self.target = target
        super().__init__(*args, **kwargs)

    def get_users(self, email):
        return [self.target] if self.target and self.target.is_active else []


def _send_reset(request, user):
    form = _AdminResetForm({"email": user.email}, target=user)
    if form.is_valid():
        form.save(request=request, use_https=request.is_secure(),
                  email_template_name="accounts/emails/password_reset.txt",
                  subject_template_name="accounts/emails/password_reset_subject.txt")


@superuser_required
def users(request):
    form = UserCreate(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        base = d["email"].split("@")[0][:140]
        username, n = base, 1
        while User.objects.filter(username__iexact=username).exists():
            n += 1
            username = f"{base}{n}"
        user = User.objects.create_user(username=username, email=d["email"], first_name=d["first_name"],
                                        last_name=d["last_name"], password=None)
        user.is_superuser = user.is_staff = d["superuser"]
        user.save()
        Profile.objects.get_or_create(user=user)
        if d["workspace"]:
            Membership.objects.create(user=user, workspace=d["workspace"], role=d["role"])
            Profile.objects.filter(user=user).update(current_workspace=d["workspace"])
        _send_reset(request, user)
        log(request, "user.created", user.email, superuser=d["superuser"], workspace=getattr(d["workspace"], "name", ""))
        messages.success(request, f"{user.email} created — we emailed them a link to choose a password.")
        return redirect("superadmin:user", user.pk)
    q = request.GET.get("q", "").strip()
    flt = request.GET.get("filter", "")
    qs = User.objects.select_related("profile").annotate(
        spaces=Count("memberships", filter=Q(memberships__active=True), distinct=True)).order_by("-is_superuser", "email")
    if q:
        qs = qs.filter(Q(email__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(username__icontains=q))
    if flt == "super":
        qs = qs.filter(is_superuser=True)
    elif flt == "inactive":
        qs = qs.filter(is_active=False)
    elif flt == "no2fa":
        qs = qs.filter(Q(profile__totp_enabled_at__isnull=True) | Q(profile__isnull=True))
    elif flt == "never":
        qs = qs.filter(last_login__isnull=True)
    elif flt == "noworkspace":
        qs = qs.filter(spaces=0)
    page = Paginator(qs, 30).get_page(request.GET.get("page"))
    return render(request, "superadmin/users.html", _ctx("users", page=page, q=q, flt=flt, form=form, filters=[
        ("", "All"), ("super", "Super admins"), ("no2fa", "Without two-step"), ("never", "Never signed in"),
        ("noworkspace", "No workspace"), ("inactive", "Deactivated")]))


def _user_sessions(user):
    rows = []
    for s in Session.objects.filter(expire_date__gt=timezone.now()):
        try:
            data = s.get_decoded()
        except Exception:
            continue
        if str(data.get("_auth_user_id")) == str(user.pk):
            rows.append(s)
    return rows


@superuser_required
def user_detail(request, pk):
    target = get_object_or_404(User, pk=pk)
    profile, _ = Profile.objects.get_or_create(user=target)
    if request.method == "POST":
        return _user_action(request, target, profile)
    return render(request, "superadmin/user_detail.html", _ctx(
        "users", u=target, profile=profile, memberships=target.memberships.select_related("workspace").order_by("workspace__name"),
        sessions=len(_user_sessions(target)), tokens=ApiToken.objects.filter(user=target).select_related("workspace"),
        workspaces=Workspace.objects.exclude(memberships__user=target).order_by("name"), roles=Membership.ROLES,
        events=PlatformEvent.objects.filter(Q(target=target.email) | Q(actor=target))[:10], is_self=target == request.user,
        signins=SignInEvent.objects.filter(user=target)[:8]))


def _user_action(request, target, profile):
    p = request.POST
    op = p.get("op")
    back = redirect("superadmin:user", target.pk)
    is_self = target == request.user
    if op in ("deactivate", "activate"):
        if is_self:
            messages.error(request, "You can't deactivate your own account.")
            return back
        if op == "deactivate" and target.is_superuser and User.objects.filter(is_superuser=True, is_active=True).count() <= 1:
            messages.error(request, "This is the last active super admin.")
            return back
        target.is_active = op == "activate"
        target.save(update_fields=["is_active"])
        if not target.is_active:
            for s in _user_sessions(target):
                s.delete()
        log(request, f"user.{op}d", target.email)
        messages.success(request, f"{target.email} is {'active' if target.is_active else 'deactivated and signed out everywhere'}.")
    elif op in ("grant_super", "revoke_super"):
        if is_self:
            messages.error(request, "Ask another super admin to change your own access.")
            return back
        target.is_superuser = target.is_staff = op == "grant_super"
        target.save(update_fields=["is_superuser", "is_staff"])
        log(request, f"user.{op}", target.email)
        messages.success(request, f"{target.email} {'is now a super admin' if target.is_superuser else 'is no longer a super admin'}.")
    elif op == "reset_2fa":
        profile.totp_secret, profile.totp_enabled_at, profile.recovery_codes = "", None, []
        profile.save(update_fields=["totp_secret", "totp_enabled_at", "recovery_codes"])
        log(request, "user.2fa_reset", target.email)
        messages.success(request, f"Two-step verification was turned off for {target.email}. They can set it up again.")
    elif op == "signout":
        n = 0
        for s in _user_sessions(target):
            if is_self and s.session_key == request.session.session_key:
                continue
            s.delete()
            n += 1
        log(request, "user.signed_out", target.email, sessions=n)
        messages.success(request, f"Signed out of {n} session(s).")
    elif op == "send_reset":
        _send_reset(request, target)
        log(request, "user.password_reset_sent", target.email)
        messages.success(request, f"A password reset link was emailed to {target.email}.")
    elif op == "revoke_tokens":
        n = ApiToken.objects.filter(user=target, revoked_at__isnull=True).update(revoked_at=timezone.now())
        log(request, "user.tokens_revoked", target.email, tokens=n)
        messages.success(request, f"{n} API token(s) revoked.")
    elif op == "add_membership":
        ws = get_object_or_404(Workspace, pk=p.get("workspace"))
        role = p.get("role") if p.get("role") in dict(Membership.ROLES) else Membership.CREATOR
        Membership.objects.update_or_create(user=target, workspace=ws, defaults={"role": role, "active": True})
        log(request, "workspace.member_added", ws.name, user=target.email, role=role)
        messages.success(request, f"{target.email} added to {ws.name}.")
    elif op == "profile":
        target.first_name = p.get("first_name", "").strip()[:150]
        target.last_name = p.get("last_name", "").strip()[:150]
        email = p.get("email", "").strip().lower()
        if email and email != target.email.lower():
            if User.objects.filter(email__iexact=email).exclude(pk=target.pk).exists():
                messages.error(request, "Another user already has this email.")
                return back
            log(request, "user.email_changed", target.email, new=email)
            target.email = email
        target.save(update_fields=["first_name", "last_name", "email"])
        messages.success(request, "Profile saved.")
    return back


# ---- settings -------------------------------------------------------------------------------------------------------
class SettingsForm(forms.ModelForm):
    announcement_until = forms.DateTimeField(required=False, widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
                                             input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"])

    class Meta:
        model = PlatformSettings
        exclude = ["features", "updated_by"]
        widgets = {"maintenance_message": forms.TextInput(), "announcement": forms.TextInput()}

    def clean_password_min_length(self):
        v = self.cleaned_data["password_min_length"]
        if v < 6 or v > 128:
            raise forms.ValidationError("Choose between 6 and 128 characters.")
        return v

    def clean_max_upload_mb(self):
        v = self.cleaned_data["max_upload_mb"]
        if not 1 <= v <= 2048:
            raise forms.ValidationError("Choose between 1 and 2048 MB.")
        return v

    def clean(self):
        d = super().clean()
        if d.get("signup_mode") == PlatformSettings.SIGNUP_DOMAINS and not (d.get("signup_domains") or "").strip():
            self.add_error("signup_domains", "List the allowed domains.")
        return d


SECTIONS = [
    ("general", "General", ["site_name", "support_email"]),
    ("signup", "Sign-up & new workspaces", ["signup_mode", "signup_domains", "default_plan", "default_plan_days",
                                           "default_credits", "max_workspaces_per_user"]),
    ("security", "Security", ["password_min_length", "require_2fa", "session_idle_minutes", "max_upload_mb",
                              "blocked_extensions", "block_private_networks"]),
    ("operations", "Announcements & maintenance", ["announcement", "announcement_level", "announcement_until",
                                                    "maintenance_mode", "maintenance_message"]),
]


@superuser_required
def settings_view(request):
    ps = get_settings()
    form = SettingsForm(request.POST or None, instance=ps)
    if request.method == "POST":
        if form.is_valid():
            before = {f: getattr(PlatformSettings.objects.get(pk=1), f) for f in form.changed_data}
            obj = form.save(commit=False)
            obj.features = {k: request.POST.get(f"feat_{k}") == "on" for k, _, _ in FEATURES}
            obj.updated_by = request.user
            obj.save()
            changed = {f: str(getattr(obj, f))[:80] for f in form.changed_data}
            if (PlatformSettings.objects.get(pk=1).features or {}) != (ps.features or {}):
                changed["features"] = ", ".join(k for k, v in obj.features.items() if not v) or "all on"
            log(request, "platform.settings", "Platform settings", **changed)
            if "maintenance_mode" in before:
                log(request, "platform.maintenance_" + ("on" if obj.maintenance_mode else "off"), "Platform")
            messages.success(request, "Platform settings saved.")
            return redirect("superadmin:settings")
        messages.error(request, "Please fix the highlighted fields.")
    sections = [{"key": k, "title": t, "fields": [form[f] for f in names]} for k, t, names in SECTIONS]
    feats = [{"key": k, "label": l, "help": h, "on": ps.feature(k)} for k, l, h in FEATURES]
    return render(request, "superadmin/settings.html", _ctx("settings", form=form, sections=sections, feats=feats, ps=ps))


# ---- background jobs ------------------------------------------------------------------------------------------------
@superuser_required
def jobs(request):
    from flows.models import BotJob, BotLog, OutboundEmail, WebhookDelivery

    if request.method == "POST":
        op = request.POST.get("op")
        if op == "tick":
            from flows import webhooks
            from flows.bots import tick

            summary = tick()
            sent = webhooks.deliver_due() if hasattr(webhooks, "deliver_due") else 0
            cache.set("scheduler-heartbeat", timezone.now(), 3600)
            log(request, "platform.jobs_run", "Background jobs", **{k: v for k, v in summary.items()})
            messages.success(request, "Background jobs ran: " + ", ".join(f"{k} {v}" for k, v in summary.items()) + f", webhooks {sent}.")
        elif op == "retry_hook":
            d = get_object_or_404(WebhookDelivery, pk=request.POST.get("delivery"))
            from flows import webhooks

            d.status, d.attempts = WebhookDelivery.PENDING, 0
            d.save(update_fields=["status", "attempts"])
            ok = webhooks.deliver(d)
            log(request, "platform.webhook_retry", d.endpoint.url, ok=ok)
            messages.success(request, "Delivered." if ok else f"Still failing: {d.error or d.response_code}.")
        elif op == "run_job":
            from flows.bots import run_bot

            job = get_object_or_404(BotJob, pk=request.POST.get("job"), done=False)
            run_bot(job.packet, job.bot, job.trigger, job.context, from_job=True)
            job.done = True
            job.save(update_fields=["done"])
            log(request, "platform.bot_job_run", job.bot.get("name", "bot"), packet=job.packet_id)
            messages.success(request, "Delayed bot ran now.")
        elif op == "cancel_job":
            job = get_object_or_404(BotJob, pk=request.POST.get("job"), done=False)
            job.done = True
            job.save(update_fields=["done"])
            log(request, "platform.bot_job_cancelled", job.bot.get("name", "bot"), packet=job.packet_id)
            messages.success(request, "Delayed bot cancelled.")
        return redirect("superadmin:jobs")
    now = timezone.now()
    day = now - dt.timedelta(days=1)
    return render(request, "superadmin/jobs.html", _ctx(
        "jobs", now=now, heartbeat=cache.get("scheduler-heartbeat"), scheduler_on=getattr(settings, "PAPERLANE_SCHEDULER", False),
        bot_jobs=BotJob.objects.filter(done=False).select_related("packet", "packet__workspace").order_by("run_at")[:50],
        failed_bots=BotLog.objects.filter(status="failed").select_related("workspace", "packet").order_by("-created_at")[:30],
        hooks=WebhookDelivery.objects.exclude(status=WebhookDelivery.OK).select_related("endpoint", "endpoint__workspace")
        .order_by("-created_at")[:30],
        counts={"bots_24": BotLog.objects.filter(created_at__gte=day).count(),
                "bots_failed_24": BotLog.objects.filter(created_at__gte=day, status="failed").count(),
                "emails_24": OutboundEmail.objects.filter(created_at__gte=day).count(),
                "hooks_24": WebhookDelivery.objects.filter(created_at__gte=day).count()}))


# ---- platform audit ------------------------------------------------------------------------------------------------
@superuser_required
def audit(request):
    q = request.GET.get("q", "").strip()
    qs = PlatformEvent.objects.select_related("actor")
    if q:
        qs = qs.filter(Q(action__icontains=q) | Q(target__icontains=q) | Q(actor_label__icontains=q))
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "superadmin/audit.html", _ctx("audit", page=page, q=q))


# ---- system ---------------------------------------------------------------------------------------------------------
@superuser_required
def system(request):
    from django.core import checks

    if request.method == "POST" and request.POST.get("op") == "clear_cache":
        cache.clear()
        log(request, "platform.cache_cleared", "Cache")
        messages.success(request, "Cache cleared.")
        return redirect("superadmin:system")
    db = settings.DATABASES["default"]
    db_size = 0
    if db["ENGINE"].endswith("sqlite3"):
        try:
            db_size = os.path.getsize(db["NAME"])
        except OSError:
            pass
    deploy = [{"level": "error" if m.is_serious() else "warning", "id": m.id, "msg": m.msg, "hint": m.hint}
              for m in checks.run_checks(include_deployment_checks=True) if m.id and m.level >= checks.WARNING]
    env = [
        ("Paperlane", "1.0"), ("Django", django.get_version()), ("Python", sys.version.split()[0]),
        ("Operating system", f"{py_platform.system()} {py_platform.release()}"),
        ("Database", f"{connection.vendor} — {db.get('NAME')}"), ("Database size", db_size),
        ("Uploaded files", _dir_size(settings.MEDIA_ROOT) if settings.MEDIA_ROOT else 0),
        ("Debug", settings.DEBUG), ("Allowed hosts", ", ".join(settings.ALLOWED_HOSTS)),
        ("Public address", getattr(settings, "SITE_URL", "") or "—"),
        ("Email backend", settings.EMAIL_BACKEND), ("Time zone", settings.TIME_ZONE),
        ("Scheduler", "in-process" if getattr(settings, "PAPERLANE_SCHEDULER", False) else "external"),
        ("Ollama", os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")),
    ]
    return render(request, "superadmin/system.html", _ctx("system", env=env, deploy=deploy, checks=health_checks()))


# ---- sign-in log ----------------------------------------------------------------------------------------------------
@superuser_required
def signins(request):
    q = request.GET.get("q", "").strip()
    flt = request.GET.get("filter", "")
    qs = SignInEvent.objects.select_related("user")
    if q:
        qs = qs.filter(Q(email__icontains=q) | Q(ip__icontains=q))
    if flt in (SignInEvent.OK, SignInEvent.FAILED, SignInEvent.LOGOUT):
        qs = qs.filter(result=flt)
    day = timezone.now() - dt.timedelta(days=1)
    failed = (SignInEvent.objects.filter(result=SignInEvent.FAILED, created_at__gte=day).values("email")
              .annotate(n=Count("id"), last=Max("created_at")).filter(n__gte=3).order_by("-n")[:10])
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "superadmin/signins.html", _ctx("signins", page=page, q=q, flt=flt, suspicious=failed,
                                                           filters=[("", "All"), ("ok", "Signed in"), ("failed", "Failed"),
                                                                    ("logout", "Signed out")]))


# ---- broadcasts -----------------------------------------------------------------------------------------------------
class BroadcastForm(forms.Form):
    title = forms.CharField(max_length=200)
    body = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}), required=False)
    link = forms.CharField(max_length=300, required=False, help_text="Optional — e.g. /inbox/ or https://…")
    workspace = forms.ModelChoiceField(Workspace.objects.order_by("name"), required=False, empty_label="Everyone (all workspaces)")
    roles = forms.MultipleChoiceField(choices=Membership.ROLES, required=False, widget=forms.CheckboxSelectMultiple,
                                      help_text="Leave empty for every role.")
    email = forms.BooleanField(required=False, label="Also send by email")

    def clean_link(self):
        link = self.cleaned_data["link"].strip()
        if link and not link.startswith(("/", "https://", "http://")):
            raise forms.ValidationError("Use a path like /inbox/ or a full https:// address.")
        return link


def broadcast_audience(ws=None, roles=None):
    users = User.objects.filter(is_active=True)
    ms = Membership.objects.filter(active=True)
    if ws:
        ms = ms.filter(workspace=ws)
    if roles:
        ms = ms.filter(role__in=roles)
    if ws or roles:
        users = users.filter(pk__in=ms.values("user_id"))
    return users.distinct()


@superuser_required
def broadcasts(request):
    from core.models import Notification
    from flows import mailer

    form = BroadcastForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        users = list(broadcast_audience(d["workspace"], d["roles"]))
        Notification.objects.bulk_create([
            Notification(user=u, workspace=d["workspace"], kind="announcement", title=d["title"][:200], body=d["body"], link=d["link"])
            for u in users])
        if d["email"]:
            for u in users:
                if u.email:
                    mailer.send(d["workspace"], u.email, "custom", {"subject": d["title"],
                                                                    "message": d["body"] + (f"\n\n{d['link']}" if d["link"] else "")})
        aud = (d["workspace"].name if d["workspace"] else "Everyone") + (
            " · " + ", ".join(dict(Membership.ROLES)[r] for r in d["roles"]) if d["roles"] else "")
        Broadcast.objects.create(title=d["title"], body=d["body"], link=d["link"], audience=aud, recipients=len(users),
                                 emailed=d["email"], sent_by=request.user)
        log(request, "platform.broadcast", d["title"], audience=aud, recipients=len(users), email=d["email"])
        messages.success(request, f"Sent to {len(users)} people{' (and by email)' if d['email'] else ''}.")
        return redirect("superadmin:broadcasts")
    return render(request, "superadmin/broadcasts.html", _ctx("broadcasts", form=form,
                                                              history=Broadcast.objects.select_related("sent_by")[:30]))


# ---- CSV exports ----------------------------------------------------------------------------------------------------
@superuser_required
def export_csv(request, what):
    import csv

    from django.http import HttpResponse

    resp = HttpResponse(content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="paperlane-{what}-{timezone.localdate()}.csv"'
    w = csv.writer(resp)
    if what == "workspaces":
        w.writerow(["ID", "Name", "Plan", "Active until", "Credits", "Members", "Workflows", "Documents", "Suspended", "Created"])
        for ws in Workspace.objects.annotate(
                members=Count("memberships", filter=Q(memberships__active=True), distinct=True),
                flows=Count("workflows", filter=Q(workflows__trashed_at__isnull=True), distinct=True),
                docs=Count("packets", distinct=True)).order_by("name"):
            w.writerow([ws.slug, ws.name, ws.plan, ws.plan_expires or "", ws.credits_limit, ws.members, ws.flows, ws.docs,
                        ws.suspended_at.date() if ws.suspended_at else "", ws.created_at.date()])
    elif what == "users":
        w.writerow(["Email", "Name", "Super admin", "Active", "Two-step", "Workspaces", "Joined", "Last sign-in"])
        for u in User.objects.select_related("profile").prefetch_related("memberships__workspace").order_by("email"):
            spaces = "; ".join(f"{m.workspace.name} ({m.get_role_display()})" for m in u.memberships.all() if m.active)
            w.writerow([u.email, u.get_full_name(), "yes" if u.is_superuser else "", "yes" if u.is_active else "no",
                        "on" if getattr(getattr(u, "profile", None), "two_factor_on", False) else "off", spaces,
                        u.date_joined.date(), u.last_login.strftime("%Y-%m-%d %H:%M") if u.last_login else ""])
    else:
        raise PermissionDenied
    log(request, "platform.exported", what)
    return resp


# ---- Support tickets (all workspaces) -------------------------------------------------------------------------------
@superuser_required
def tickets(request):
    from flows.models import SupportTicket

    if request.method == "POST":
        t = get_object_or_404(SupportTicket, pk=request.POST.get("ticket"))
        stamp, by = timezone.now().isoformat(), request.user.get_full_name() or request.user.email
        new = request.POST.get("status")
        if new in dict(SupportTicket.STATUSES) and new != t.status:
            t.status = new
            t.resolved_at = timezone.now() if new in (SupportTicket.RESOLVED, SupportTicket.CLOSED) else None
            t.notes = t.notes + [{"by": by, "at": stamp, "staff": True, "text": f"Status changed to {t.get_status_display()}."}]
        if "assignee" in request.POST:
            uid = request.POST.get("assignee")
            t.assignee = get_user_model().objects.filter(pk=uid, is_superuser=True).first() if uid else None
        text = request.POST.get("note", "").strip()[:4000]
        if text:
            t.notes = t.notes + [{"by": by, "at": stamp, "staff": True, "text": text}]
            t.first_response_at = t.first_response_at or timezone.now()
            from core.models import Notification

            if t.created_by_id:
                Notification.objects.create(user=t.created_by, workspace=t.workspace, kind="ticket",
                                            title=f"Support replied on {t.ref}", body=text[:300], link=t.get_absolute_url())
        t.save()
        log(request, "platform.ticket", t.ref, status=t.status, noted=bool(text))
        messages.success(request, f"{t.ref} updated.")
        return redirect(request.get_full_path())
    qs = SupportTicket.objects.select_related("workspace", "packet", "created_by", "assignee")
    status, severity, q = request.GET.get("status", "active"), request.GET.get("severity", ""), request.GET.get("q", "").strip()
    if status == "active":
        qs = qs.filter(status__in=SupportTicket.ACTIVE)
    elif status:
        qs = qs.filter(status=status)
    if severity:
        qs = qs.filter(severity=severity)
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(workspace__name__icontains=q) | Q(summary__icontains=q))
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    rows = sorted(qs[:300], key=lambda t: (order.get(t.severity, 9), -t.created_at.timestamp()))
    active = SupportTicket.objects.filter(status__in=SupportTicket.ACTIVE)
    if request.GET.get("mine") == "1":
        rows = [t for t in rows if t.assignee_id == request.user.pk]
    from django.db.models import Count, Q as _Q

    from core import help_content as hc
    from core.models import HelpFeedback

    fb = (HelpFeedback.objects.values("article").annotate(yes=Count("id", filter=_Q(helpful=True)), no=Count("id", filter=_Q(helpful=False)))
          .order_by("-no")[:8])
    help_rows = [{**r, "title": (hc.get(*r["article"].split("/")) or {}).get("title", r["article"]), "url": f"/help/{r['article']}/"} for r in fb if r["no"]]
    return render(request, "superadmin/tickets.html", _ctx(
        "tickets", help_rows=help_rows, rows=rows, staff=get_user_model().objects.filter(is_superuser=True, is_active=True).order_by("email"),
        overdue=sum(1 for t in active.select_related(None) if t.response_overdue), status=status, severity=severity, q=q, statuses=SupportTicket.STATUSES,
        severities=SupportTicket.SEVERITIES, counts={s: active.filter(severity=s).count() for s, _ in SupportTicket.SEVERITIES}))


# ---- Single sign-on (OpenID Connect) ---------------------------------------------------------------------------------
class SSOForm(forms.ModelForm):
    class Meta:
        from accounts.models import SSOProvider

        model = SSOProvider
        fields = ["name", "kind", "issuer", "client_id", "client_secret", "scopes", "allowed_domains", "create_users",
                  "workspace", "default_role", "enabled"]
        widgets = {"client_secret": forms.PasswordInput(render_value=True)}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["default_role"] = forms.ChoiceField(choices=[c for c in Membership.ROLES if c[0] != Membership.OWNER])
        self.fields["allowed_domains"].required = True

    def clean_allowed_domains(self):
        v = self.cleaned_data["allowed_domains"]
        doms = [d.strip().lower().lstrip("@") for d in v.split(",") if d.strip()]
        bad = [d for d in doms if "." not in d or " " in d]
        if not doms or bad:
            raise forms.ValidationError("List the email domains this provider may sign in, e.g. example.com, example.org")
        return ", ".join(doms)


@superuser_required
def sso(request, pk=None):
    from accounts.models import SSOProvider
    from accounts.sso import SSOError, discovery

    obj = get_object_or_404(SSOProvider, pk=pk) if pk else None
    if request.method == "POST" and request.POST.get("do") == "delete" and obj:
        log(request, "platform.sso_deleted", obj.name)
        obj.delete()
        messages.success(request, "Provider removed.")
        return redirect("superadmin:sso")
    if request.method == "POST" and request.POST.get("do") == "test" and obj:
        try:
            cache.delete(f"oidc-disc:{obj.issuer}")
            d = discovery(obj)
            messages.success(request, f"Discovery OK — authorization at {d['authorization_endpoint'].split('/')[2]}, "
                                      f"userinfo {'available' if d.get('userinfo_endpoint') else 'missing'}.")
        except SSOError as exc:
            messages.error(request, str(exc))
        return redirect("superadmin:sso_edit", obj.pk)
    form = SSOForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        p = form.save()
        log(request, "platform.sso_saved", p.name, issuer=p.issuer, domains=p.allowed_domains, create_users=p.create_users)
        messages.success(request, f"“{p.name}” saved.")
        return redirect("superadmin:sso_edit", p.pk)
    return render(request, "superadmin/sso.html", _ctx("sso", form=form, obj=obj, providers=SSOProvider.objects.all(),
                                                       callback=request.build_absolute_uri(reverse("accounts:sso_callback"))))
