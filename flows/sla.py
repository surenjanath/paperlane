"""Step deadlines (SLAs) with escalation, and out-of-office delegation.

Step settings::

    "sla": {"amount": 2, "unit": "business_days" | "days" | "hours",
            "escalate": "notify" | "reassign" | "none",       # what happens when the deadline passes
            "escalate_to": "lead@example.com" | "Group name",  # who is told / who takes over
            "tag": "#Overdue"}                                 # optional tag added to the document
"""
import datetime as dt

from django.contrib.auth import get_user_model
from django.utils import timezone

UNITS = [("hours", "hours"), ("days", "calendar days"), ("business_days", "business days")]
ESCALATIONS = [("notify", "Notify someone"), ("reassign", "Reassign the step"), ("none", "Only mark it overdue")]


def config(step):
    sla = ((step or {}).get("settings") or {}).get("sla") or {}
    try:
        amount = float(sla.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0:
        return None
    return {**sla, "amount": amount, "unit": sla.get("unit") if sla.get("unit") in dict(UNITS) else "days"}


def add_business_days(start, days):
    """Add working days (Mon–Fri), keeping the time of day; fractions become hours of a 24 h day."""
    whole, frac = int(days), days - int(days)
    current = start
    while current.weekday() >= 5:  # starting on a weekend counts from Monday
        current += dt.timedelta(days=1)
    added = 0
    while added < whole:
        current += dt.timedelta(days=1)
        if current.weekday() < 5:
            added += 1
    return current + dt.timedelta(hours=24 * frac)


def due_at(step, start=None):
    cfg = config(step)
    if not cfg:
        return None
    start = start or timezone.now()
    if cfg["unit"] == "hours":
        return start + dt.timedelta(hours=cfg["amount"])
    if cfg["unit"] == "business_days":
        return add_business_days(timezone.localtime(start), cfg["amount"])
    return start + dt.timedelta(days=cfg["amount"])


def describe(step):
    cfg = config(step)
    if not cfg:
        return ""
    amount = int(cfg["amount"]) if cfg["amount"] == int(cfg["amount"]) else cfg["amount"]
    unit = dict(UNITS)[cfg["unit"]].split(" (")[0]
    if amount == 1:
        unit = unit.replace("days", "day").replace("hours", "hour")
    return f"Due in {amount} {unit}"


# ---- out of office -------------------------------------------------------------------------------
def delegate_for(user, workspace, depth=0):
    """The person who should get work meant for ``user`` today (follows chains, max 3), or None."""
    from accounts.models import Membership, Profile

    if user is None or depth >= 3:
        return None
    profile = Profile.objects.filter(user=user).select_related("ooo_delegate").first()
    if not profile or not profile.out_of_office() or not profile.ooo_delegate_id or profile.ooo_delegate_id == user.pk:
        return None
    target = profile.ooo_delegate
    if not Membership.objects.filter(user=target, workspace=workspace, active=True).exists():
        return None
    return delegate_for(target, workspace, depth + 1) or target


def apply_delegation(packet, assignees):
    """Swap assignees who are out of office for their delegates. Returns the new list (dicts gain delegated_from)."""
    if packet.is_test:
        return assignees
    User = get_user_model()
    out = []
    for a in assignees:
        user = a.get("user") or (User.objects.filter(email__iexact=a["email"]).first() if a.get("email") else None)
        target = delegate_for(user, packet.workspace) if user else None
        if target is None:
            out.append(a)
            continue
        out.append({**a, "email": target.email, "user": target,
                    "name": f"{target.get_full_name() or target.email} (for {a.get('name') or user.email})",
                    "delegated_from": user.email})
    return out


def group_recipients(group, workspace):
    """Emails for a group step: members who are out of office are replaced by their delegates."""
    emails, users = [], []
    for m in group.members.select_related("user"):
        target = delegate_for(m.user, workspace) or m.user
        if target.email and target.email not in emails:
            emails.append(target.email)
            users.append(target)
    return emails, users


# ---- overdue processing (called from tick) ---------------------------------------------------------
def process_overdue(now=None):
    from accounts.models import Group

    from . import engine, mailer
    from .models import Packet, StepRun, Tag

    now = now or timezone.now()
    count = 0
    runs = (StepRun.objects.filter(status__in=[StepRun.PENDING, StepRun.OPENED], due_at__lte=now, escalated_at__isnull=True,
                                   packet__status=Packet.IN_PROGRESS)
            .select_related("packet", "packet__workspace", "packet__workflow", "assignee_user", "assignee_group"))
    for run in runs:
        packet = run.packet
        step = packet.step(run.step_key) or {}
        cfg = config(step) or {}
        run.escalated_at = now
        run.save(update_fields=["escalated_at"])
        count += 1
        late = timezone.localtime(run.due_at).strftime("%b %d, %H:%M")
        engine.audit(packet.workspace, "step.overdue", f"{run.step_name} was due {late}", packet=packet)
        tag = str(cfg.get("tag") or "").strip()
        if tag:
            t, _ = Tag.objects.get_or_create(workspace=packet.workspace, name=tag[:80])
            packet.tags.add(t)
        # the people who hold the step
        holders = [run.assignee_user] if run.assignee_user else (
            [m.user for m in run.assignee_group.members.select_related("user")] if run.assignee_group else [])
        for u in holders:
            engine.notify(u, packet.workspace, f"Overdue: {packet.name}", f"{run.step_name} was due {late}", run.fill_url(),
                          kind="assigned")
        if run.assignee_email:
            mailer.send(packet.workspace, run.assignee_email, "custom", {
                "subject": f"Overdue: {packet.name}",
                "message": f"The step “{run.step_name}” was due {late}. Please complete it: {run.absolute_fill_url()}"}, packet=packet)
        action, target = cfg.get("escalate") or "notify", str(cfg.get("escalate_to") or "").strip()
        owner = packet.workflow.created_by
        if owner:
            engine.notify(owner, packet.workspace, f"Overdue: {packet.name}", f"{run.step_name} — {run.assignee_name or run.assignee_email}",
                          f"/documents/{packet.pk}/", kind="assigned")
        if not target or action == "none":
            continue
        group = Group.objects.filter(workspace=packet.workspace, name__iexact=target).first()
        if action == "reassign":
            previous = run.assignee_email or run.assignee_name
            if group:
                run.assignee_group, run.assignee_email, run.assignee_user, run.assignee_name = group, "", None, group.name
                emails, users = group_recipients(group, packet.workspace)
            else:
                user = get_user_model().objects.filter(email__iexact=target).first()
                run.assignee_group, run.assignee_email, run.assignee_user, run.assignee_name = None, target, user, target
                emails, users = [target], [user] if user else []
            run.claimed_by, run.claimed_at = None, None
            run.due_at = due_at(step, now)
            run.escalated_at = None if run.due_at else now  # the new holder gets a fresh deadline
            run.save()
            if emails:
                mailer.send(packet.workspace, emails, "custom", {
                    "subject": f"Escalated to you: {packet.name}",
                    "message": f"“{run.step_name}” was overdue with {previous} and has been reassigned to you: {run.absolute_fill_url()}"},
                    packet=packet)
            for u in users:
                engine.notify(u, packet.workspace, f"Escalated: {packet.name}", run.step_name, run.fill_url(), kind="assigned")
            engine.audit(packet.workspace, "step.reassigned", f"{run.step_name} → {run.assignee_name} (overdue escalation)",
                         packet=packet)
        else:
            emails = group_recipients(group, packet.workspace)[0] if group else [target]
            if emails:
                mailer.send(packet.workspace, emails, "custom", {
                    "subject": f"Overdue: {packet.name}",
                    "message": f"“{run.step_name}” assigned to {run.assignee_name or run.assignee_email} was due {late} and "
                               f"is still open. Open the document: {engine.absolute(f'/documents/{packet.pk}/')}"}, packet=packet)
    return count
