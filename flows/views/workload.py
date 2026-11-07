"""Team workload (owners, supervisors, auditors): open steps per person and per group queue, overdue work,
throughput, and bulk reassign / release."""
import datetime as dt

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.utils import timezone

from accounts.models import Group, Membership

from .. import engine, mailer
from ..models import Packet, StepRun

User = get_user_model()


def _open_runs(ws):
    return (StepRun.objects.filter(packet__workspace=ws, status__in=[StepRun.PENDING, StepRun.OPENED],
                                   packet__status=Packet.IN_PROGRESS, packet__is_test=False, packet__trashed_at__isnull=True)
            .select_related("packet", "packet__workflow", "assignee_user", "assignee_group", "claimed_by")
            .order_by("created_at"))


@login_required
def workload(request):
    m = request.membership
    if not m or not m.can_view_all:
        raise PermissionDenied("Only owners, supervisors and auditors can see team workload.")
    ws = request.workspace
    if request.method == "POST":
        if not m.can_manage:
            raise PermissionDenied
        return _bulk(request, ws)
    now = timezone.now()
    runs = list(_open_runs(ws))
    users_by_email = {u.email.lower(): u for u in User.objects.filter(memberships__workspace=ws, memberships__active=True)}
    people, queues = {}, {}

    def person(u):
        return people.setdefault(u.pk, {"user": u, "open": 0, "overdue": 0, "oldest": None, "done_7": 0, "avg_hours": None})

    for r in runs:
        holder = r.claimed_by or r.assignee_user or (users_by_email.get(r.assignee_email.lower()) if r.assignee_email else None)
        r.holder = holder
        r.overdue = bool(r.due_at and r.due_at < now)
        r.age = now - r.created_at
        if holder is not None:
            p = person(holder)
            p["open"] += 1
            p["overdue"] += r.overdue
            p["oldest"] = p["oldest"] or r.created_at
        elif r.assignee_group_id:
            q = queues.setdefault(r.assignee_group_id, {"group": r.assignee_group, "waiting": 0, "overdue": 0, "oldest": None})
            q["waiting"] += 1
            q["overdue"] += r.overdue
            q["oldest"] = q["oldest"] or r.created_at
    week = now - dt.timedelta(days=7)
    for r in (StepRun.objects.filter(packet__workspace=ws, status=StepRun.COMPLETED, completed_at__gte=week, packet__is_test=False)
              .select_related("assignee_user", "claimed_by").only("created_at", "completed_at", "assignee_user", "claimed_by",
                                                                   "assignee_email")):
        u = r.claimed_by or r.assignee_user or (users_by_email.get(r.assignee_email.lower()) if r.assignee_email else None)
        if u is None:
            continue
        p = person(u)
        p["done_7"] += 1
        hours = (r.completed_at - r.created_at).total_seconds() / 3600
        p["_h"] = p.get("_h", 0) + hours
    for p in people.values():
        if p["done_7"]:
            p["avg"] = dt.timedelta(hours=p.pop("_h", 0) / p["done_7"])
    for mm in Membership.objects.filter(workspace=ws, active=True).select_related("user"):
        person(mm.user)  # everyone appears, even with nothing open
    flt = request.GET.get("filter", "")
    shown = runs
    if flt == "overdue":
        shown = [r for r in runs if r.overdue]
    elif flt == "unclaimed":
        shown = [r for r in runs if r.holder is None and r.assignee_group_id]
    elif flt.startswith("user:"):
        shown = [r for r in runs if r.holder and str(r.holder.pk) == flt[5:]]
    elif flt.startswith("group:"):
        shown = [r for r in runs if str(r.assignee_group_id) == flt[6:]]
    return render(request, "flows/workload.html", {
        "nav": "workload", "people": sorted(people.values(), key=lambda p: (-p["overdue"], -p["open"], p["user"].email)),
        "queues": sorted(queues.values(), key=lambda q: -q["waiting"]), "runs": shown[:300], "total": len(runs), "flt": flt,
        "overdue": sum(1 for r in runs if r.overdue), "unclaimed": sum(1 for r in runs if r.holder is None and r.assignee_group_id),
        "members": Membership.objects.filter(workspace=ws, active=True).select_related("user").order_by("user__first_name"),
        "groups": Group.objects.filter(workspace=ws), "can_manage": m.can_manage})


def _bulk(request, ws):
    ids = [int(x) for x in request.POST.getlist("run") if x.isdigit()]
    runs = list(_open_runs(ws).filter(pk__in=ids))
    action = request.POST.get("action")
    if not runs:
        messages.error(request, "Select at least one step.")
        return redirect(request.get_full_path())
    if action == "release":
        n = 0
        for r in runs:
            if r.claimed_by_id:
                r.release()
                n += 1
        messages.success(request, f"{n} step(s) put back in their group queue.")
    elif action == "reassign":
        target = request.POST.get("target", "")
        group = user = None
        if target.startswith("g:"):
            group = Group.objects.filter(workspace=ws, pk=target[2:]).first()
        elif target.startswith("u:"):
            user = User.objects.filter(pk=target[2:], memberships__workspace=ws, memberships__active=True).first()
        if not (group or user):
            messages.error(request, "Choose who should take the selected steps.")
            return redirect(request.get_full_path())
        for r in runs:
            before = r.claimed_by or r.assignee_user or r.assignee_group or r.assignee_email
            if group:
                r.assignee_group, r.assignee_user, r.assignee_email, r.assignee_name = group, None, "", group.name
            else:
                r.assignee_group, r.assignee_user, r.assignee_email = None, user, user.email
                r.assignee_name = user.get_full_name() or user.email
            r.claimed_by, r.claimed_at = None, None
            r.save()
            engine.audit(ws, "step.reassigned", f"{r.step_name} → {r.assignee_name} (team workload, was {before})",
                         actor=request.user, packet=r.packet, request=request)
            if user:
                engine.notify(user, ws, f"{r.packet.name} is waiting for you", r.step_name, r.fill_url(), kind="assigned")
        if user and user.email:
            mailer.send(ws, user.email, "custom", {
                "subject": f"{len(runs)} task(s) were assigned to you",
                "message": "\n".join(f"• {r.packet.name} — {r.step_name}: {r.absolute_fill_url()}" for r in runs)})
        messages.success(request, f"{len(runs)} step(s) reassigned to {group.name if group else user.email}.")
    return redirect(request.get_full_path())


@login_required
def workload_summary(request):
    """✦ This week: the AI operations note and the facts behind it (JSON)."""
    from django.core.exceptions import PermissionDenied
    from django.http import JsonResponse

    from ..digests import ops_summary

    m = request.membership
    if not m or not m.can_view_all:
        raise PermissionDenied
    return JsonResponse(ops_summary(m.workspace, refresh=request.GET.get("refresh") == "1"))
