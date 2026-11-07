"""Workspace audit trail: categories, filters, per-day activity, sensitive actions, and grouping of repeated events."""
import datetime as dt
from collections import Counter, OrderedDict

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from flows.models import AuditEvent, Workflow

# category → (label, icon, action prefixes)
CATEGORIES = OrderedDict([
    ("documents", ("Documents", "documents", ("packet.", "step.", "branch.", "form."))),
    ("workflows", ("Workflows", "workflow", ("workflow.", "bot.", "autostart."))),
    ("team", ("Team", "team", ("team.", "group.", "invite", "member"))),
    ("security", ("Security", "shield", ("login", "logout", "security.", "2fa", "token", "session", "password", "api."))),
    ("ai", ("Automation & AI", "sparkle", ("ticket.", "ai.", "connector.", "webhook.", "packet.ask_ai", "workflow.ai_"))),
    ("settings", ("Settings", "gear", ("workspace.", "settings.", "theme", "email_template", "domain", "billing"))),
])
SENSITIVE = ("trash", "delete", "remove", "block", "role", "revoke", "cancel", "2fa", "token", "unlock", "takeover",
             "taken_over", "group_deleted", "export")


LABELS = {"packet.created": "Started", "packet.completed": "Completed", "packet.trash": "Moved to trash", "packet.restore": "Restored",
          "packet.archive": "Archived", "packet.revise": "Sent back", "packet.revised": "Revised", "packet.remind": "Reminder",
          "packet.downloaded": "Downloaded", "packet.comment": "Comment", "packet.ask_ai": "Asked AI", "packet.form": "Form submitted",
          "packet.cancel": "Cancelled", "packet.lock": "Locked", "packet.unlock": "Unlocked", "packet.reassign": "Reassigned",
          "step.sent": "Sent", "step.opened": "Opened", "step.completed": "Step done", "step.declined": "Declined",
          "step.jump": "Jumped", "step.remind": "Reminder", "step.claimed": "Took task", "step.released": "Released",
          "step.taken_over": "Took over", "step.delegated": "Delegated", "step.overdue": "Overdue", "branch.matched": "Branch",
          "workflow.published": "Published", "workflow.created": "Created", "workflow.rename": "Renamed",
          "workflow.ai_edit": "AI edit", "workflow.ai_undo": "AI undo", "workflow.ai_built": "AI built", "workflow.link": "Link",
          "workflow.trash": "Moved to trash", "workflow.duplicate": "Duplicated", "workflow.access": "Access changed",
          "ticket.created": "Ticket filed", "ticket.status": "Ticket update", "form.created": "Form created",
          "connector.created": "Connector", "team.role": "Role changed", "team.block": "Blocked", "team.activate": "Activated",
          "team.remove": "Removed", "team.groups": "Groups changed", "team.position": "Position", "team.group_saved": "Group saved",
          "team.group_deleted": "Group deleted"}


def label_of(action):
    return LABELS.get(action) or (action or "").split(".")[-1].replace("_", " ").capitalize()


def category_of(action):
    a = action or ""
    if a.startswith(("packet.ask_ai", "workflow.ai_")):
        return "ai"
    for key, (_, _, prefixes) in CATEGORIES.items():
        if a.startswith(prefixes):
            return key
    return "settings"


def is_sensitive(action):
    return any(w in (action or "") for w in SENSITIVE)


def _cat_q(key):
    q = Q()
    for p in CATEGORIES[key][2]:
        q |= Q(action__startswith=p)
    if key == "documents":
        q &= ~Q(action__startswith="packet.ask_ai")
    if key == "workflows":
        q &= ~Q(action__startswith="workflow.ai_")
    return q


def _sens_q():
    q = Q()
    for w in SENSITIVE:
        q |= Q(action__contains=w)
    return q


PRESETS = [("today", "Today", 0), ("7", "7 days", 6), ("30", "30 days", 29), ("90", "90 days", 89)]


@login_required
def audit(request):
    from flows.exports import audit_csv

    if not request.membership.can_view_all:
        messages.error(request, "Only owners, supervisors and auditors can view the workspace audit trail.")
        return redirect("core:dashboard")
    g = request.GET
    today = timezone.localdate()

    def _date(key, default):
        try:
            return dt.date.fromisoformat(g.get(key, ""))
        except ValueError:
            return default

    preset = g.get("range", "" if g.get("from") or g.get("to") else "7")
    days_back = dict((k, d) for k, _, d in PRESETS).get(preset)
    if days_back is not None:
        start, end = today - dt.timedelta(days=days_back), today
    else:
        start, end = _date("from", today - dt.timedelta(days=6)), _date("to", today)
    if start > end:
        start, end = end, start
    base = AuditEvent.objects.filter(workspace=request.workspace, created_at__date__gte=start, created_at__date__lte=end)
    events = base.select_related("workflow", "packet", "actor", "actor__profile")
    q = g.get("q", "").strip()
    if q:
        events = events.filter(Q(actor_label__icontains=q) | Q(action__icontains=q) | Q(detail__icontains=q)
                               | Q(packet__name__icontains=q) | Q(workflow__name__icontains=q) | Q(ip__icontains=q))
    person, wf_id, cat = g.get("person", ""), g.get("workflow", ""), g.get("cat", "")
    if person:
        events = events.filter(actor_label=person)
    if wf_id.isdigit():
        events = events.filter(Q(workflow_id=wf_id) | Q(packet__workflow_id=wf_id))
    sensitive = g.get("sensitive") == "1"
    scoped = events  # before the category / sensitive filters, for the chip counts
    if cat in CATEGORIES:
        events = events.filter(_cat_q(cat))
    if sensitive:
        events = events.filter(_sens_q())
    if g.get("format") == "csv":
        resp = HttpResponse(audit_csv(events.order_by("-created_at")), content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="audit-{start}-{end}.csv"'
        return resp

    action_counts = Counter(dict(scoped.values_list("action").annotate(n=Count("id"))))
    cat_counts = Counter()
    sens_count = 0
    for a, n in action_counts.items():
        cat_counts[category_of(a)] += n
        sens_count += n if is_sensitive(a) else 0
    # per-day activity for the chart
    per_day = dict(events.annotate(d=TruncDate("created_at")).values_list("d").annotate(n=Count("id")))
    span = (end - start).days + 1
    chart = []
    if span <= 92:
        peak = max(per_day.values(), default=0) or 1
        for i in range(span):
            d = start + dt.timedelta(days=i)
            n = per_day.get(d, 0)
            chart.append({"d": d, "n": n, "h": max(4, round(100 * n / peak)) if n else 0})

    page = Paginator(events.order_by("-created_at"), 150).get_page(g.get("page"))
    group_on = g.get("group", "1") != "0"
    days = OrderedDict()
    for e in page.object_list:
        e.cat = category_of(e.action)
        e.sensitive = is_sensitive(e.action)
        e.label = label_of(e.action)
        day = timezone.localtime(e.created_at).date()
        rows = days.setdefault(day, [])
        last = rows[-1] if rows else None
        # group a run of events by the same person on the same document within 10 minutes
        if (group_on and last and last["packet_id"] and last["packet_id"] == e.packet_id and last["actor"] == e.actor_label
                and last["events"][-1].created_at - e.created_at <= dt.timedelta(minutes=10) and not e.sensitive):
            last["events"].append(e)
            continue
        rows.append({"packet_id": e.packet_id, "actor": e.actor_label, "events": [e]})
    stats = {"total": events.count(), "people": events.exclude(actor_label="").values("actor_label").distinct().count(),
             "documents": events.exclude(packet__isnull=True).values("packet").distinct().count(), "sensitive": sens_count}
    people = list(base.exclude(actor_label="").values_list("actor_label", flat=True).distinct().order_by("actor_label"))
    params = g.copy()
    params.pop("page", None)
    return render(request, "core/audit.html", {
        "nav": "audit", "days": days, "start": start, "end": end, "q": q, "page": page, "today": today,
        "yesterday": today - dt.timedelta(days=1), "preset": preset, "presets": PRESETS, "chart": chart,
        "categories": [(k, l, icon, cat_counts.get(k, 0)) for k, (l, icon, _) in CATEGORIES.items()],
        "cat": cat, "cat_info": {k: (l, icon) for k, (l, icon, _) in CATEGORIES.items()}, "sensitive": sensitive,
        "person": person, "people": people, "wf_id": wf_id, "group_on": group_on, "stats": stats,
        "workflows": Workflow.objects.filter(workspace=request.workspace, kind="workflow").order_by("name").only("pk", "name"),
        "qs": params.urlencode()})
