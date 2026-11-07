"""Dashboard (Home + Analytics), Inbox, global Search, Notifications, Archive and Trash."""
from collections import OrderedDict

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.db.models.functions import TruncDay, TruncMonth, TruncWeek
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import Contact, UsageEvent
from flows.models import AuditEvent, Packet, PacketRevision, StepRun, Workflow
from flows.presets import PRESETS

from .models import DashboardWidget, Notification
from core.utils import safe_next

from flows.queries_util import RUNS_PREFETCH  # noqa: E402


def seed_widgets(user, workspace):
    if DashboardWidget.objects.filter(user=user, workspace=workspace).exists():
        return
    DashboardWidget.objects.create(user=user, workspace=workspace, title="Data Ingression", order=0,
                                   metrics=["new_documents", "completed_documents", "sends", "bot_executions", "steps_completed"],
                                   chart="area")
    DashboardWidget.objects.create(user=user, workspace=workspace, title="New documents", order=1, metrics=["new_documents"], chart="area")
    DashboardWidget.objects.create(user=user, workspace=workspace, title="Completed documents", order=2,
                                   metrics=["completed_documents"], chart="bar")


def landing(request):
    """The public landing page (also at /welcome/ for signed-in people)."""
    from flows.form_templates import TEMPLATES

    return render(request, "core/landing.html", {"form_templates": TEMPLATES, "presets": PRESETS})


def dashboard(request):
    """Signed in: the workspace dashboard. Visitors: the public landing page."""
    if not request.user.is_authenticated:
        return landing(request)
    if getattr(request, "workspace", None) is None:
        return redirect("accounts:onboarding")
    return render(request, "core/dashboard.html", {"presets": PRESETS, "tab": "home", "nav": "dashboard"})


@login_required
def analytics(request):
    seed_widgets(request.user, request.workspace)
    widgets = DashboardWidget.objects.filter(user=request.user, workspace=request.workspace)
    return render(request, "core/analytics.html", {
        "widgets": widgets, "tab": "analytics", "nav": "dashboard", "metrics": DashboardWidget.METRICS,
        "aggregations": DashboardWidget.AGGREGATIONS, "charts": DashboardWidget.CHARTS,
        "workflows": Workflow.objects.visible_to(request.membership).live().filter(kind="workflow"),
    })


def _metric_qs(metric, ws, start, wf_ids):
    wf_filter = {"workflow_id__in": wf_ids} if wf_ids else {}
    if metric == "new_documents":
        return Packet.objects.filter(workspace=ws, is_test=False, created_at__gte=start, **wf_filter), "created_at"
    if metric == "completed_documents":
        return Packet.objects.filter(workspace=ws, is_test=False, completed_at__gte=start, **wf_filter), "completed_at"
    if metric == "declined_documents":
        return AuditEvent.objects.filter(workspace=ws, action="step.declined", created_at__gte=start, **wf_filter), "created_at"
    if metric == "sends":
        return UsageEvent.objects.filter(workspace=ws, kind="send", created_at__gte=start, **wf_filter), "created_at"
    if metric == "bot_executions":
        return UsageEvent.objects.filter(workspace=ws, kind="bot", created_at__gte=start, **wf_filter), "created_at"
    if metric == "steps_completed":
        f = {"packet__workflow_id__in": wf_ids} if wf_ids else {}
        return StepRun.objects.filter(packet__workspace=ws, status="completed", completed_at__gte=start, **f), "completed_at"
    if metric == "data_ingression":
        f = {"packet__workflow_id__in": wf_ids} if wf_ids else {}
        return PacketRevision.objects.filter(packet__workspace=ws, created_at__gte=start, **f), "created_at"
    return Packet.objects.none(), "created_at"


@login_required
def widget_data(request, pk):
    w = get_object_or_404(DashboardWidget, pk=pk, user=request.user)
    days = int(request.GET.get("days") or w.range_days)
    agg = request.GET.get("aggregation") or w.aggregation
    end = timezone.localdate()
    start_date = end - timezone.timedelta(days=days - 1)
    start = timezone.make_aware(timezone.datetime.combine(start_date, timezone.datetime.min.time()))
    trunc = {"week": TruncWeek, "month": TruncMonth}.get(agg, TruncDay)
    # buckets
    buckets = OrderedDict()
    d = start_date
    while d <= end:
        if agg == "week":
            key = d - timezone.timedelta(days=d.weekday())
        elif agg == "month":
            key = d.replace(day=1)
        else:
            key = d
        buckets.setdefault(key, 0)
        d += timezone.timedelta(days=1)
    labels = [k.strftime("%m/%d") if agg != "month" else k.strftime("%b %Y") for k in buckets]
    series = []
    names = dict(DashboardWidget.METRICS)
    for metric in w.metrics or ["new_documents"]:
        qs, field = _metric_qs(metric, request.workspace, start, w.workflow_ids)
        counts = dict(buckets)
        for row in qs.annotate(b=trunc(field)).values("b").annotate(n=Count("id")):
            if row["b"] is None:
                continue
            key = timezone.localtime(row["b"]).date() if hasattr(row["b"], "tzinfo") and row["b"].tzinfo else (
                row["b"].date() if hasattr(row["b"], "date") else row["b"])
            if key in counts:
                counts[key] += row["n"]
        series.append({"label": names.get(metric, metric), "data": list(counts.values())})
    return JsonResponse({"labels": labels, "series": series, "title": w.title, "chart": w.chart,
                         "range": f"{start_date:%b %d, %Y} - {end:%b %d, %Y}"})


@login_required
def widget_edit(request, pk=None):
    w = get_object_or_404(DashboardWidget, pk=pk, user=request.user) if pk else None
    if request.method == "POST":
        if request.POST.get("delete") and w:
            w.delete()
            messages.success(request, "Widget removed.")
            return redirect("core:analytics")
        w = w or DashboardWidget(user=request.user, workspace=request.workspace,
                                 order=DashboardWidget.objects.filter(user=request.user, workspace=request.workspace).count())
        w.title = request.POST.get("title") or "Widget"
        w.metrics = request.POST.getlist("metrics") or ["new_documents"]
        w.workflow_ids = [int(x) for x in request.POST.getlist("workflows") if x.isdigit()]
        w.range_days = int(request.POST.get("range_days") or 7)
        w.aggregation = request.POST.get("aggregation", "day")
        w.chart = request.POST.get("chart", "area")
        w.save()
        messages.success(request, "Widget saved.")
        return redirect("core:analytics")
    return render(request, "core/partials/widget_form.html", {
        "w": w, "metrics": DashboardWidget.METRICS, "aggregations": DashboardWidget.AGGREGATIONS,
        "charts": DashboardWidget.CHARTS, "selected": (w.metrics if w else ["new_documents"]),
        "workflows": Workflow.objects.visible_to(request.membership).live().filter(kind="workflow"),
        "ranges": [(7, "Last 7 days"), (14, "Last 14 days"), (30, "Last 30 days"), (90, "Last 90 days"), (365, "Last year")],
    })


@login_required
@require_POST
def widgets_reorder(request):
    for i, pk in enumerate(request.POST.getlist("order")):
        DashboardWidget.objects.filter(user=request.user, pk=pk).update(order=i)
    return JsonResponse({"ok": True})


# ---- Live updates -------------------------------------------------------------------------------
def _epoch(dt):
    return int(dt.timestamp()) if dt else 0


@login_required
def live(request):
    """Cheap revision stamp plus the sidebar counts. Open pages poll this and refresh when it changes."""
    ws = getattr(request, "workspace", None)
    if ws is None:
        return JsonResponse({"rev": "0", "waiting": 0, "unread": 0, "tickets": 0, "urgent": False})
    from django.db.models import Count, Max, Q

    from flows.models import FormSubmission, IntakeForm, SupportTicket

    from .queries import waiting_packet_count

    audit = AuditEvent.objects.filter(workspace=ws).aggregate(m=Max("id"))["m"] or 0
    note = Notification.objects.filter(user=request.user).aggregate(m=Max("id"))["m"] or 0
    unread = Notification.objects.filter(user=request.user, read=False).count()
    pkt = Packet.objects.filter(workspace=ws).aggregate(m=Max("updated_at"))["m"]
    wf = Workflow.objects.filter(workspace=ws).aggregate(m=Max("updated_at"))["m"]
    form = IntakeForm.objects.filter(workspace=ws).aggregate(m=Max("updated_at"))["m"]
    runs = StepRun.objects.filter(packet__workspace=ws).aggregate(
        n=Count("id"),
        open=Count("id", filter=Q(status__in=["pending", "opened"])),
        claimed=Count("id", filter=Q(claimed_by__isnull=False, status__in=["pending", "opened"])),
        done=Max("completed_at"),
        took=Max("claimed_at"),
    )
    subs = FormSubmission.objects.filter(form__workspace=ws).aggregate(
        n=Count("id"),
        started=Count("id", filter=Q(status="started")),
        failed=Count("id", filter=Q(status="failed")),
    )
    ticket_at = SupportTicket.objects.filter(workspace=ws).aggregate(m=Max("updated_at"))["m"]
    rev = ":".join(str(x) for x in (
        audit, note, unread, _epoch(pkt), _epoch(wf), _epoch(form),
        runs["n"] or 0, runs["open"] or 0, runs["claimed"] or 0, _epoch(runs["done"]), _epoch(runs["took"]),
        subs["n"] or 0, subs["started"] or 0, subs["failed"] or 0, _epoch(ticket_at),
    ))
    waiting = waiting_packet_count(request.user, ws)
    tickets = SupportTicket.objects.filter(workspace=ws, status__in=SupportTicket.ACTIVE)
    membership = getattr(request, "membership", None)
    if membership and not (membership.can_manage or membership.role == "auditor"):
        tickets = tickets.filter(created_by=request.user)
    resp = JsonResponse({
        "rev": rev, "waiting": waiting, "unread": unread,
        "tickets": tickets.count(), "urgent": tickets.filter(severity__in=["high", "critical"]).exists(),
    })
    resp["Cache-Control"] = "no-store"
    return resp


# ---- Inbox ------------------------------------------------------------------------------------
@login_required
def inbox(request):
    tab = request.GET.get("tab", "all")
    from .queries import my_runs, waiting_packet_count

    runs = my_runs(request.user, request.workspace)
    if tab == "waiting":
        runs = runs.filter(status__in=["pending", "opened"], packet__status=Packet.IN_PROGRESS)
    elif tab == "completed":
        runs = runs.filter(status="completed")
    else:
        runs = runs.exclude(status__in=["skipped", "revoked"])
    q = request.GET.get("q", "").strip()
    if q:
        runs = runs.filter(Q(packet__name__icontains=q) | Q(packet__workflow__name__icontains=q) | Q(step_name__icontains=q))
    # newest activity first, one row per document, 25 per page — rows then use prefetched data (no per-row queries)
    from django.core.paginator import Paginator
    from django.db.models import Max

    from flows.views.packets import attach_next_due

    order = list(runs.values("packet_id").annotate(last=Max("created_at")).order_by("-last").values_list("packet_id", flat=True)[:1000])
    page = Paginator(order, 25).get_page(request.GET.get("page"))
    by_id = Packet.objects.filter(pk__in=list(page.object_list)).select_related("workflow", "created_by") \
        .prefetch_related("tags", RUNS_PREFETCH(), "revisions").in_bulk()
    packets = attach_next_due([by_id[i] for i in page.object_list if i in by_id])
    waiting = waiting_packet_count(request.user, request.workspace)
    return render(request, "core/inbox.html", {"packets": packets, "page": page, "tab": tab, "q": q, "nav": "inbox", "waiting": waiting})


# ---- Search ------------------------------------------------------------------------------------
@login_required
def search(request):
    q = request.GET.get("q", "").strip()
    results = []
    if len(q) >= 1:
        m = request.membership
        for wf in Workflow.objects.visible_to(m).live().filter(Q(name__icontains=q) | Q(tags__name__icontains=q)).exclude(kind="document").distinct()[:8]:
            results.append({"type": wf.get_kind_display(), "title": wf.name, "url": wf.get_absolute_url(),
                            "meta": f"Updated {timezone.localtime(wf.updated_at):%b %d, %Y}"})
        for p in Packet.objects.visible_to(m).live().filter(Q(name__icontains=q) | Q(runs__assignee_email__icontains=q)).distinct()[:8]:
            results.append({"type": "Document", "title": p.name, "url": p.get_absolute_url(), "meta": p.get_status_display()})
        for c in Contact.objects.filter(workspace=request.workspace).filter(Q(name__icontains=q) | Q(email__icontains=q))[:6]:
            results.append({"type": "Contact", "title": c.name, "url": reverse("accounts:contacts") + f"?q={c.email}", "meta": c.email})
    if request.headers.get("Accept", "").startswith("application/json") or request.GET.get("format") == "json":
        return JsonResponse({"results": results})
    return render(request, "core/search.html", {"q": q, "results": results})


# ---- Notifications ------------------------------------------------------------------------------
@login_required
def notifications(request):
    qs = Notification.objects.filter(user=request.user)
    if request.GET.get("panel"):
        today = timezone.localdate()
        items = list(qs[:40])
        groups = [("Today", [n for n in items if timezone.localtime(n.created_at).date() == today]),
                  ("Previous 7 days", [n for n in items if today - timezone.timedelta(days=7) <= timezone.localtime(n.created_at).date() < today]),
                  ("Older", [n for n in items if timezone.localtime(n.created_at).date() < today - timezone.timedelta(days=7)])]
        return render(request, "core/partials/notifications_panel.html", {"groups": [g for g in groups if g[1]]})
    return render(request, "core/notifications.html", {"items": qs[:300]})


@login_required
@require_POST
def notifications_read(request):
    qs = Notification.objects.filter(user=request.user, read=False)
    if request.POST.get("id"):
        qs = qs.filter(pk=request.POST["id"])
    qs.update(read=True)
    if request.headers.get("x-requested-with") == "fetch":
        return JsonResponse({"ok": True})
    return safe_next(request, "core:notifications")


@login_required
def notification_open(request, pk):
    n = get_object_or_404(Notification, pk=pk, user=request.user)
    n.read = True
    n.save(update_fields=["read"])
    return redirect(n.link or "core:notifications")


# ---- Archive & Trash ----------------------------------------------------------------------------------
@login_required
def archive(request):
    m = request.membership
    return render(request, "core/bin.html", {
        "mode": "archive", "nav": "archive",
        "workflows": Workflow.objects.visible_to(m).filter(archived_at__isnull=False, trashed_at__isnull=True),
        "packets": Packet.objects.visible_to(m).filter(archived_at__isnull=False, trashed_at__isnull=True).select_related("workflow")[:300],
    })


from .audit_view import audit  # noqa: E402,F401  (workspace audit trail)


@login_required
def trash(request):
    m = request.membership
    return render(request, "core/bin.html", {
        "mode": "trash", "nav": "trash",
        "workflows": Workflow.objects.visible_to(m).filter(trashed_at__isnull=False),
        "packets": Packet.objects.visible_to(m).filter(trashed_at__isnull=False).select_related("workflow")[:300],
    })


@login_required
@require_POST
def empty_trash(request):
    if not request.membership.can_manage:
        messages.error(request, "Only owners and supervisors can empty the Trash.")
        return redirect("core:trash")
    m = request.membership
    n1, _ = Packet.objects.visible_to(m).filter(trashed_at__isnull=False).delete()
    n2, _ = Workflow.objects.visible_to(m).filter(trashed_at__isnull=False).delete()
    messages.success(request, "Trash emptied.")
    return redirect("core:trash")


def static_page(request, page):
    pages = {
        "release-notes": "core/pages/release_notes.html",
        "terms": "core/pages/terms.html", "privacy": "core/pages/privacy.html",
    }
    if page not in pages:
        from django.http import Http404

        raise Http404
    layout = "base.html" if request.user.is_authenticated and getattr(request, "membership", None) else "core/pages/_public_base.html"
    docs = [("release-notes", "Release notes", "bell"), ("terms", "Terms of Service", "documents"), ("privacy", "Privacy Notice", "lock")]
    return render(request, pages[page], {"layout": layout, "page": page, "docs": docs, "updated": "October 16, 2026"})


def healthz(request):
    """Liveness/readiness for load balancers and uptime monitors: database, cache and the jobs heartbeat."""
    from django.core.cache import cache
    from django.db import connection
    from django.http import JsonResponse

    checks, ok = {}, True
    try:
        with connection.cursor() as c:
            c.execute("SELECT 1")
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["database"], ok = f"error: {type(exc).__name__}", False
    try:
        cache.set("healthz", 1, 30)
        checks["cache"] = "ok" if cache.get("healthz") == 1 else "error"
    except Exception as exc:  # noqa: BLE001
        checks["cache"], ok = f"error: {type(exc).__name__}", False
    beat = cache.get("scheduler-heartbeat")
    age = (timezone.now() - beat).total_seconds() if beat else None
    checks["jobs"] = "ok" if age is not None and age < 300 else ("stale" if age else "no heartbeat yet")
    last = cache.get("last-backup")
    checks["last_backup"] = last["at"] if last else None
    return JsonResponse({"status": "ok" if ok else "error", "checks": checks}, status=200 if ok else 503)
