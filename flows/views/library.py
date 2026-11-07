"""Workflows & Templates libraries, workflow page, run/distribute and workflow-level actions."""
import csv
import io

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import Group, Membership
from core.models import RecentItem

from .. import engine, services
from ..exports import audit_csv
from ..models import Packet, Tag, Workflow, WorkflowVersion
from ..permissions import can_edit_workflow, get_workflow, require
from ..presets import PRESETS, create_from_preset

from flows.queries_util import RUNS_PREFETCH  # noqa: E402


def _library(request, kind):
    m = request.membership
    qs = Workflow.objects.visible_to(m).live().filter(kind=kind).annotate(n_packets=Count("packets", filter=Q(packets__trashed_at__isnull=True, packets__archived_at__isnull=True, packets__is_test=False)))
    q = request.GET.get("q", "").strip()
    show = request.GET.get("show", "all")
    hidden = request.GET.get("hidden") == "1"
    sort = request.GET.get("sort", "updated")
    tag = request.GET.get("tag")
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(tags__name__icontains=q) | Q(description__icontains=q)).distinct()
    if not hidden:
        qs = qs.filter(is_hidden=False)
    if show == "mine":
        qs = qs.filter(created_by=request.user)
    elif show == "shared":
        qs = qs.exclude(created_by=request.user)
    if tag:
        qs = qs.filter(tags__name=tag)
    qs = qs.order_by({"name": "name", "created": "-created_at", "documents": "-n_packets"}.get(sort, "-updated_at"))
    items = list(qs.prefetch_related("tags"))
    if kind == Workflow.WORKFLOW:
        _attach_triggers(items)
    cutoff = timezone.now() - timezone.timedelta(days=30)
    groups = []
    if sort == "updated":
        recent = [w for w in items if w.updated_at >= cutoff]
        older = [w for w in items if w.updated_at < cutoff]
        if recent:
            groups.append(("Previous 30 days", recent))
        if older:
            groups.append(("Older than 30 days", older))
    elif items:
        groups.append(("", items))
    total = Workflow.objects.visible_to(m).live().filter(kind=kind).count()
    return render(request, "flows/library.html", {
        "kind": kind, "groups": groups, "total": total, "q": q, "show": show, "hidden": hidden, "sort": sort,
        "all_tags": Tag.objects.filter(workspace=request.workspace), "nav": f"{kind}s",
    })


def _attach_triggers(items):
    """Name what starts each workflow: a form, an autostart (Google Sheets, a schedule, email…) or the shareable link."""
    from .. import autostart as auto_mod
    from ..models import IntakeForm

    pks = [w.pk for w in items]
    forms = {}
    for form in IntakeForm.objects.filter(workflow_id__in=pks, archived_at__isnull=True).only("workflow_id", "name"):
        forms.setdefault(form.workflow_id, []).append(form.name)
    source_ids = set()
    for wf in items:
        for item in auto_mod.items(wf):
            if item.get("type") == "workflow":
                try:
                    source_ids.add(int(item.get("source_workflow") or 0))
                except (TypeError, ValueError):
                    pass
    source_ids.discard(0)
    names = dict(Workflow.objects.filter(pk__in=source_ids).values_list("pk", "name"))
    for wf in items:
        triggers = [{"label": f"Form · {name}", "off": False} for name in forms.get(wf.pk, [])]
        for item in auto_mod.items(wf):
            triggers.append({"label": auto_mod.trigger_label(item, names), "off": not item.get("enabled", True)})
        if wf.link_enabled:
            triggers.append({"label": "Shareable link", "off": False})
        wf.triggers = triggers
        wf.trigger_title = ", ".join(t["label"] + (" (paused)" if t["off"] else "") for t in triggers) or "No trigger"


@login_required
def workflows(request):
    return _library(request, Workflow.WORKFLOW)


@login_required
@require_POST
def workflows_bulk(request):
    """Archive, hide, or trash the workflows checked on the library page."""
    if not request.membership or not request.membership.can_create:
        raise PermissionDenied
    action = request.POST.get("action")
    if action not in ("archive", "trash", "hide", "unhide"):
        raise PermissionDenied("Unknown action")
    now = timezone.now()
    n = 0
    for wf in Workflow.objects.visible_to(request.membership).filter(
            pk__in=request.POST.getlist("ids"), kind=Workflow.WORKFLOW, trashed_at__isnull=True):
        if not can_edit_workflow(request.membership, wf):
            continue
        if action == "archive":
            wf.archived_at = now
        elif action == "trash":
            wf.trashed_at = now
        elif action == "hide":
            wf.is_hidden = True
        else:
            wf.is_hidden = False
        wf.save()
        engine.audit(request.workspace, f"workflow.{action}", wf.name, actor=request.user, workflow=wf, request=request)
        n += 1
    done = {"archive": "archived", "trash": "moved to Trash", "hide": "hidden", "unhide": "visible again"}[action]
    messages.success(request, f"{n} workflow{'' if n == 1 else 's'} {done}." if n else "No workflows were changed.")
    from core.utils import safe_url
    return redirect(safe_url(request, request.META.get("HTTP_REFERER"), reverse("flows:workflows")))


@login_required
def templates(request):
    return _library(request, Workflow.TEMPLATE)


@login_required
@require("create")
@require_POST
def create(request, kind):
    if kind not in (Workflow.WORKFLOW, Workflow.TEMPLATE):
        raise PermissionDenied
    name = request.POST.get("name") or ("Untitled workflow" if kind == Workflow.WORKFLOW else "Untitled template")
    wf = services.create_workflow(request.workspace, request.user, name=name, kind=kind)
    engine.audit(request.workspace, "workflow.created", wf.name, actor=request.user, workflow=wf, request=request)
    if kind == Workflow.TEMPLATE:
        doc = wf.documents.first()
        return redirect("flows:editor", wf.pk, doc.pk)
    return redirect("flows:builder", wf.pk)


@login_required
@require("create")
@require_POST
def create_preset(request, key):
    if key not in PRESETS:
        raise PermissionDenied
    wf = create_from_preset(key, request.workspace, request.user)
    engine.audit(request.workspace, "workflow.created", f"From preset {PRESETS[key]['name']}", actor=request.user,
                 workflow=wf, request=request)
    messages.success(request, f"“{wf.name}” was created from a ready-made automation. Set the recipients and publish it.")
    return redirect("flows:builder", wf.pk)


@login_required
def workflow_detail(request, pk):
    wf = get_workflow(request, pk, include_deleted=True)
    RecentItem.objects.update_or_create(user=request.user, workflow=wf)
    packets = wf.packets.live().filter(is_test=False)
    status = request.GET.get("status")
    if status:
        packets = packets.filter(status=status)
    at_step = (request.GET.get("step") or "")[:20]
    at_step_name = ""
    if at_step:  # documents waiting at one step (the builder's live counts link here)
        from ..models import StepRun

        packets = packets.filter(status="in_progress", runs__step_key=at_step,
                                 runs__status__in=[StepRun.PENDING, StepRun.OPENED]).distinct()
        at_step_name = wf.steps.filter(key=at_step).values_list("name", flat=True).first() or "that step"
    tests = wf.packets.filter(is_test=True).count()
    return render(request, "flows/workflow_detail.html", {
        "wf": wf, "packets": packets.prefetch_related("tags", RUNS_PREFETCH())[:100], "packet_count": wf.packets.live().filter(is_test=False).count(),
        "tests": tests, "can_edit": can_edit_workflow(request.membership, wf), "status": status,
        "at_step": at_step, "at_step_name": at_step_name,
        "versions": wf.versions.all()[:20], "nav": "workflows" if wf.kind != "template" else "templates",
        "hook_url": engine.absolute(reverse("flows:hook", args=[wf.hook_token])),
        "link_url": engine.absolute(reverse("flows:public_start", args=[wf.link_token])),
        "field_names": sorted({c[2] for c in services.field_choices(wf)}, key=str.lower),
        "dup": (wf.options or {}).get("duplicates") or {},
    })


@login_required
@require_POST
def workflow_action(request, pk, action):
    wf = get_workflow(request, pk, include_deleted=True)
    m = request.membership
    editable = can_edit_workflow(m, wf)
    from core.utils import safe_url

    back = safe_url(request, request.POST.get("next") or request.META.get("HTTP_REFERER"), reverse("flows:workflows"))
    if action == "duplicate":
        if not m.can_create:
            raise PermissionDenied
        clone = services.duplicate(wf, request.user)
        engine.audit(request.workspace, "workflow.duplicated", f"Copy of {wf.name}", actor=request.user, workflow=clone, request=request)
        messages.success(request, f"Duplicated as “{clone.name}”.")
        return redirect(back)
    if not editable:
        raise PermissionDenied("You can't change this workflow.")
    now = timezone.now()
    label = {"archive": "archived", "trash": "moved to Trash", "hide": "hidden", "unhide": "is visible again",
             "restore": "restored", "delete": "deleted permanently"}.get(action, "updated")
    if action == "tags":
        names = [t.strip() for t in request.POST.get("tags", "").split(",") if t.strip()]
        wf.tags.set([Tag.objects.get_or_create(workspace=request.workspace, name=n[:80])[0] for n in names])
    elif action == "archive":
        wf.archived_at = now
    elif action == "trash":
        wf.trashed_at = now
    elif action == "hide":
        wf.is_hidden = True
    elif action == "unhide":
        wf.is_hidden = False
    elif action == "restore":
        wf.archived_at = None
        wf.trashed_at = None
    elif action == "delete":
        if wf.trashed_at is None:
            raise PermissionDenied("Move the workflow to Trash first.")
        name = wf.name
        wf.delete()
        messages.success(request, f"“{name}” was deleted permanently.")
        return redirect(back)
    elif action == "rename":
        wf.name = (request.POST.get("name") or wf.name)[:200]
        wf.description = request.POST.get("description", wf.description)
    elif action == "link":
        wf.link_enabled = request.POST.get("enabled") == "1"
    elif action == "regenerate_link":
        import secrets

        wf.link_token = secrets.token_urlsafe()
    elif action == "welcome":
        wf.welcome_enabled = request.POST.get("enabled") == "1"
        wf.welcome_message = request.POST.get("message", "")
    elif action == "duplicates":
        fields = [f.strip() for f in request.POST.get("fields", "").replace("\n", ",").split(",") if f.strip()][:5]
        try:
            days = max(0, min(3650, int(request.POST.get("days") or 0)))
        except ValueError:
            days = 0
        wf.options = {**(wf.options or {}), "duplicates": {"fields": fields, "days": days,
                                                          "notify": request.POST.get("notify") == "1"}}
        if not fields:
            wf.options.pop("duplicates")
        messages.success(request, "Duplicate check saved." if fields else "Duplicate check turned off.")
    elif action == "consent":
        wf.esign_consent = request.POST.get("enabled") == "1"
        wf.has_unpublished_changes = True
    elif action == "access":
        wf.access = request.POST.get("access", wf.access)
        wf.shared_users.set(Membership.objects.filter(workspace=request.workspace, pk__in=request.POST.getlist("users")).values_list("user", flat=True))
        wf.shared_groups.set(Group.objects.filter(workspace=request.workspace, pk__in=request.POST.getlist("groups")))
    elif action == "restore_version":
        version = get_object_or_404(WorkflowVersion, workflow=wf, pk=request.POST.get("version"))
        from .. import undo

        undo.record(wf, request.user, f"Restore version {version.number}")  # can be undone from the builder
        services.load_snapshot(wf, version.snapshot)
        messages.success(request, f"Version {version.number} restored into the editor. Publish to make it live.")
    else:
        raise PermissionDenied("Unknown action")
    wf.save()
    engine.audit(request.workspace, f"workflow.{action}", wf.name, actor=request.user, workflow=wf, request=request)
    if action not in ("rename", "tags", "restore_version"):
        messages.success(request, f"“{wf.name}” {label}.")
    if action in ("trash", "archive") and str(pk) in back:
        back = reverse("flows:workflows")
    return redirect(back)


# ---- run / distribute -----------------------------------------------------------------
@login_required
@require("run")
def run(request, pk):
    wf = get_workflow(request, pk)
    first = next((s for s in wf.ordered_steps() if s.kind == "recipient"), None)
    if request.method == "POST":
        if not wf.is_published or wf.has_unpublished_changes:
            if not can_edit_workflow(request.membership, wf):
                if not wf.is_published:
                    messages.error(request, "This workflow hasn't been published yet.")
                    return redirect(wf)
            else:
                services.publish(wf, request.user)
        assignee = None
        override = request.POST.get("first_email", "").strip()
        if override and first is not None:
            from django.contrib.auth import get_user_model

            assignee = {"email": override, "name": request.POST.get("first_name") or override,
                        "user": get_user_model().objects.filter(email__iexact=override).first()}
        packet = engine.start_packet(wf, user=request.user, source="manual", name=request.POST.get("name") or None,
                                     first_assignee=assignee, request=request,
                                     tags=[t for t in request.POST.get("tags", "").split(",") if t.strip()])
        if assignee:  # explicit override → email the invitation now
            _send_invites(packet)
        run_ = packet.runs.filter(status__in=["pending", "opened"]).first()
        if run_ and run_.can_act(request.user):
            return redirect("flows:fill", run_.token)
        messages.success(request, f"“{packet.name}” was sent.")
        return redirect("flows:packet_detail", packet.pk)
    return render(request, "flows/run.html", {
        "wf": wf, "first": first, "default_name": engine.default_packet_name(wf),
        "needs_publish": not wf.is_published or wf.has_unpublished_changes, "nav": "workflows"})


def _send_invites(packet):
    from .. import mailer

    for r in packet.runs.filter(status="pending"):
        if r.assignee_email:
            mailer.send(packet.workspace, r.assignee_email, "invite", {
                "user_name": r.assignee_name, "workflow_name": packet.workflow.name, "document_name": packet.name,
                "link": r.absolute_fill_url(), "message": ""}, packet=packet)


@login_required
@require("run")
def test_run(request, pk):
    if request.method != "POST":  # starting a test creates a document — never on a plain link (CSRF)
        return redirect("flows:builder", pk)
    wf = get_workflow(request, pk, edit=True)
    packet = engine.start_packet(wf, user=request.user, source="test", is_test=True,
                                 first_assignee={"email": request.user.email, "name": engine.actor_label(request.user), "user": request.user},
                                 request=request, name=f"[TEST] {wf.name}")
    run_ = packet.runs.filter(status="pending").first()
    if run_:
        return redirect("flows:fill", run_.token)
    return redirect("flows:packet_detail", packet.pk)


@login_required
@require("run")
def send_bulk(request, pk):
    wf = get_workflow(request, pk)
    result = None
    if request.method == "POST":
        upload = request.FILES.get("csv")
        text = upload.read().decode("utf-8-sig") if upload else request.POST.get("csv_text", "")
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows:
            messages.error(request, "The CSV file is empty or has no header row.")
        else:
            if not wf.is_published or wf.has_unpublished_changes:
                if can_edit_workflow(request.membership, wf):
                    services.publish(wf, request.user)
            created = 0
            from django.contrib.auth import get_user_model

            User = get_user_model()
            for row in rows[:2000]:
                email = (row.pop("email", None) or row.pop("Email", None) or "").strip()
                name = (row.pop("name", None) or row.pop("Name", None) or "").strip()
                doc_name = row.pop("document_name", None)
                assignee = {"email": email, "name": name or email, "user": User.objects.filter(email__iexact=email).first()} if email else None
                packet = engine.start_packet(wf, user=request.user, source="bulk", prefill={k: v for k, v in row.items() if v},
                                             first_assignee=assignee, name=doc_name or None, request=request)
                if assignee:
                    _send_invites(packet)
                created += 1
            result = created
            messages.success(request, f"{created} document(s) sent in bulk.")
    fields = services.field_choices(wf)
    return render(request, "flows/send_bulk.html", {"wf": wf, "fields": fields, "result": result, "nav": "workflows"})


@login_required
def sample_csv(request, pk):
    wf = get_workflow(request, pk)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["email", "name"] + [name for _, _, name, _ in services.field_choices(wf)])
    resp = HttpResponse(buf.getvalue(), content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="{wf.name}-bulk-sample.csv"'
    return resp


@login_required
def workflow_audit(request, pk):
    wf = get_workflow(request, pk, include_deleted=True)
    events = wf.audit_events.select_related("packet")
    if request.GET.get("format") == "csv":
        resp = HttpResponse(audit_csv(events), content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="audit-{wf.pk}.csv"'
        return resp
    return render(request, "flows/audit.html", {"wf": wf, "events": events[:500], "nav": "workflows",
                                                "title": f"Audit trail · {wf.name}"})


@login_required
def workflow_bots_log(request, pk):
    wf = get_workflow(request, pk, include_deleted=True)
    return render(request, "flows/bots_log.html", {"wf": wf, "logs": wf.bot_logs.select_related("packet").only(
        "created_at", "bot_name", "trigger", "status", "message", "packet__id", "packet__name")[:500],
                                                   "nav": "workflows", "title": f"Bots log · {wf.name}"})


@login_required
def manage_access(request, pk):
    wf = get_workflow(request, pk, edit=True)
    return render(request, "flows/partials/manage_access.html", {
        "wf": wf, "members": Membership.objects.filter(workspace=request.workspace, active=True).select_related("user"),
        "groups": Group.objects.filter(workspace=request.workspace),
        "shared_user_ids": set(wf.shared_users.values_list("id", flat=True)),
        "shared_group_ids": set(wf.shared_groups.values_list("id", flat=True)),
    })


@login_required
def packet_count_badge(request):
    """Small JSON used by the sidebar to show how many documents await the user."""
    from django.http import JsonResponse

    from core.queries import waiting_packet_count

    return JsonResponse({"waiting": waiting_packet_count(request.user, request.workspace)})


@login_required
def workflow_export(request, pk):
    import json

    from ..importers import export_workflow

    wf = get_workflow(request, pk, include_deleted=True)
    if request.GET.get("format") == "blueprint":  # readable by people and AI assistants; importable too
        from ..blueprint import export_workflow as export_blueprint

        data, suffix = export_blueprint(wf), "blueprint"
    else:
        data, suffix = export_workflow(wf), "paperlane"
    resp = HttpResponse(json.dumps(data, indent=1, default=str), content_type="application/json")
    resp["Content-Disposition"] = f'attachment; filename="{wf.name[:60]}.{suffix}.json"'
    engine.audit(request.workspace, "workflow.exported", wf.name, actor=request.user, workflow=wf, request=request)
    return resp


@login_required
@require("create")
def workflow_import(request):
    from ..importers import import_any, load_file

    if request.method == "POST":
        upload = request.FILES.get("file")
        try:
            if not upload:
                raise ValueError("Choose a file to import.")
            wf, report = import_any(load_file(upload), request.workspace, request.user)
        except (ValueError, KeyError, TypeError) as exc:
            messages.error(request, f"Import failed: {exc}")
            return redirect("flows:workflows")
        request.session[f"import_report_{wf.pk}"] = report
        engine.audit(request.workspace, "workflow.imported", wf.source_ref or "file", actor=request.user, workflow=wf, request=request)
        messages.success(request, f"Imported “{wf.name}”: {len(report.get('steps', []))} steps, {len(report.get('bots', []))} bots.")
        return redirect("flows:import_report", wf.pk)
    return render(request, "flows/partials/import_modal.html")


@login_required
def import_report(request, pk):
    wf = get_workflow(request, pk)
    return render(request, "flows/import_report.html", {"wf": wf, "report": request.session.get(f"import_report_{wf.pk}") or {},
                                                       "nav": "workflows"})


def _hours(td):
    return td.total_seconds() / 3600


def _fmt_hours(h):
    if h is None:
        return "—"
    if h < 1:
        return f"{max(1, round(h * 60))} min"
    if h < 48:
        return f"{h:.1f} h"
    return f"{h / 24:.1f} days"


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2


@login_required
def workflow_insights(request, pk):
    """Where documents wait: per-step time, work in progress, deadlines and who holds the open work."""
    import datetime as dt
    from collections import Counter, defaultdict

    from ..models import StepRun

    wf = get_workflow(request, pk)
    days = request.GET.get("days", "90")
    since = timezone.now() - dt.timedelta(days=int(days)) if days.isdigit() else None
    packets = Packet.objects.filter(workflow=wf, is_test=False, trashed_at__isnull=True)
    if since:
        packets = packets.filter(created_at__gte=since)
    status_counts = Counter(packets.values_list("status", flat=True))
    cycle = [_hours(c - s) for s, c in packets.filter(status=Packet.COMPLETED, completed_at__isnull=False)
             .values_list("created_at", "completed_at")]
    runs = StepRun.objects.filter(packet__in=packets).values_list(
        "step_key", "step_name", "status", "created_at", "completed_at", "due_at", "assignee_name", "assignee_email")
    now = timezone.now()
    per = defaultdict(lambda: {"runs": 0, "done": [], "open": 0, "overdue": 0, "declined": 0, "with_due": 0, "on_time": 0,
                               "open_age": []})
    names, holders = {}, Counter()
    for key, name, status, created, completed, due, aname, aemail in runs:
        d = per[key]
        names.setdefault(key, name)
        d["runs"] += 1
        if status == "completed" and completed:
            d["done"].append(_hours(completed - created))
            if due:
                d["with_due"] += 1
                d["on_time"] += completed <= due
        elif status in ("pending", "opened"):
            d["open"] += 1
            d["open_age"].append(_hours(now - created))
            holders[aname or aemail or "Anyone with the link"] += 1
            if due and due <= now:
                d["overdue"] += 1
        elif status == "declined":
            d["declined"] += 1
    order = [s.key for s in wf.ordered_steps() if s.kind == "recipient"]
    keys = [k for k in order if k in per] + sorted(k for k in per if k not in order)
    current = {s.key: s for s in wf.steps.all()}
    rows = []
    for k in keys:
        d = per[k]
        med = _median(d["done"])
        rows.append({"key": k, "name": current[k].name if k in current else names[k], "removed": k not in current,
                     "runs": d["runs"], "completed": len(d["done"]), "open": d["open"], "overdue": d["overdue"],
                     "declined": d["declined"], "median_h": med, "avg_h": sum(d["done"]) / len(d["done"]) if d["done"] else None,
                     "median": _fmt_hours(med), "avg": _fmt_hours(sum(d["done"]) / len(d["done"]) if d["done"] else None),
                     "oldest_open": _fmt_hours(max(d["open_age"])) if d["open_age"] else "—",
                     "on_time": round(100 * d["on_time"] / d["with_due"]) if d["with_due"] else None,
                     "sla": current[k].sla_text if k in current else ""})
    timed = [r for r in rows if r["median_h"] is not None]
    slowest = max(timed, key=lambda r: r["median_h"]) if len(timed) >= 2 else None  # only meaningful with something to compare
    busiest = max(rows, key=lambda r: r["open"], default=None)
    top = max([r["median_h"] or 0 for r in rows] + [0.0001])
    top_open = max([r["open"] for r in rows] + [1])
    for r in rows:
        r["bar"] = round(100 * (r["median_h"] or 0) / top)
        r["open_bar"] = round(100 * r["open"] / top_open)
    # weekly trend (last 12 weeks)
    weeks = []
    start = (timezone.localdate() - dt.timedelta(days=timezone.localdate().weekday())) - dt.timedelta(weeks=11)
    started = Counter(timezone.localtime(c).date() - dt.timedelta(days=timezone.localtime(c).weekday())
                      for c in packets.filter(created_at__date__gte=start).values_list("created_at", flat=True))
    finished = Counter(timezone.localtime(c).date() - dt.timedelta(days=timezone.localtime(c).weekday())
                       for c in packets.filter(completed_at__date__gte=start).values_list("completed_at", flat=True))
    peak = max([1] + list(started.values()) + list(finished.values()))
    for i in range(12):
        wk = start + dt.timedelta(weeks=i)
        weeks.append({"date": wk, "label": wk.strftime("%b %d"), "started": started.get(wk, 0), "finished": finished.get(wk, 0),
                      "s_h": round(100 * started.get(wk, 0) / peak), "f_h": round(100 * finished.get(wk, 0) / peak)})
    total = sum(status_counts.values())
    # funnel: how many documents reached each step, and where the ones that didn't finish stopped
    reached, stopped = defaultdict(set), defaultdict(lambda: {"declined": 0, "cancelled": 0, "waiting": 0})
    last_step = {}
    for pid, key, status, created in StepRun.objects.filter(packet__in=packets).order_by("created_at").values_list(
            "packet_id", "step_key", "status", "created_at"):
        reached[key].add(pid)
        last_step[pid] = (key, status)
    for pid, pstatus in packets.values_list("pk", "status"):
        if pid in last_step and pstatus in (Packet.DECLINED, Packet.CANCELLED, Packet.IN_PROGRESS):
            stopped[last_step[pid][0]][{"declined": "declined", "cancelled": "cancelled"}.get(pstatus, "waiting")] += 1
    funnel = []
    for k in keys:
        n = len(reached.get(k, ()))
        funnel.append({"name": next((r["name"] for r in rows if r["key"] == k), k), "reached": n,
                       "pct": round(100 * n / total) if total else 0, **stopped[k]})
    # week by week: deadline success and the slowest step
    weekly = defaultdict(lambda: {"due": 0, "on_time": 0, "spans": defaultdict(list)})
    for key, name, status, created, completed, due, _a, _e in runs:
        if status != "completed" or not completed or timezone.localtime(completed).date() < start:
            continue
        wk = timezone.localtime(completed).date() - dt.timedelta(days=timezone.localtime(completed).weekday())
        w = weekly[wk]
        w["spans"][current[key].name if key in current else name].append(_hours(completed - created))
        if due:
            w["due"] += 1
            w["on_time"] += completed <= due
    for wrow in weeks:
        w = weekly.get(wrow["date"])
        wrow["on_time"] = round(100 * w["on_time"] / w["due"]) if w and w["due"] else None
        slow = max(((n, _median(v)) for n, v in w["spans"].items()), key=lambda x: x[1], default=None) if w else None
        wrow["slowest"] = f"{slow[0]} · {_fmt_hours(slow[1])}" if slow else ""
    total = sum(status_counts.values())
    if request.GET.get("format") == "csv":
        out = io.StringIO()
        wr = csv.writer(out)
        wr.writerow(["Steps"])
        wr.writerow(["Step", "Sent", "Completed", "Open", "Overdue", "Declined", "Median hours", "On time %", "Reached %",
                     "Stopped: declined", "Stopped: cancelled", "Waiting here"])
        for r, f in zip(rows, funnel):
            wr.writerow([r["name"], r["runs"], r["completed"], r["open"], r["overdue"], r["declined"],
                         round(r["median_h"], 2) if r["median_h"] is not None else "", r["on_time"] if r["on_time"] is not None else "",
                         f["pct"], f["declined"], f["cancelled"], f["waiting"]])
        wr.writerow([])
        wr.writerow(["Week", "Started", "Completed", "On time %", "Slowest step"])
        for w in weeks:
            wr.writerow([w["label"], w["started"], w["finished"], w["on_time"] if w["on_time"] is not None else "", w["slowest"]])
        resp = HttpResponse(out.getvalue(), content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="{wf.name[:60]} insights.csv"'
        return resp
    return render(request, "flows/insights.html", {
        "funnel": funnel,
        "wf": wf, "nav": "workflows", "days": days, "rows": rows, "slowest": slowest, "busiest": busiest,
        "holders": holders.most_common(8), "weeks": weeks, "total": total,
        "counts": {k: status_counts.get(k, 0) for k in ("in_progress", "completed", "declined", "cancelled")},
        "completion_rate": round(100 * status_counts.get("completed", 0) / total) if total else None,
        "cycle_median": _fmt_hours(_median(cycle)), "cycle_avg": _fmt_hours(sum(cycle) / len(cycle) if cycle else None),
        "overdue_now": sum(r["overdue"] for r in rows), "open_now": sum(r["open"] for r in rows),
    })


@login_required
def workflow_compare(request, pk):
    """Compare two versions: ?a=<version number>&b=<version number or "draft">."""
    from .. import versions as vs

    wf = get_workflow(request, pk)
    published = list(wf.versions.all())
    if not published:
        return render(request, "flows/compare.html", {"wf": wf, "rows": [], "versions": [], "nav": "workflows"})

    def snap(ref):
        if ref == "draft":
            return wf.build_snapshot(), "Current draft"
        v = next((x for x in published if str(x.number) == str(ref)), None)
        return (v.snapshot, f"Version {v.number}") if v else (None, "")
    b_ref = request.GET.get("b") or ("draft" if wf.has_unpublished_changes else str(published[0].number))
    a_ref = request.GET.get("a") or (str(published[0].number) if b_ref == "draft" else str(published[1].number) if len(published) > 1 else str(published[0].number))
    (a, a_label), (b, b_label) = snap(a_ref), snap(b_ref)
    rows = vs.diff(a, b) if a is not None and b is not None else []
    counts = {k: sum(1 for r in rows if r["op"] == k) for k in ("added", "changed", "removed")}
    return render(request, "flows/compare.html", {
        "wf": wf, "rows": rows, "versions": published, "a_ref": a_ref, "b_ref": b_ref, "a_label": a_label, "b_label": b_label,
        "counts": counts, "nav": "workflows"})


@login_required
def key_dates(request):
    """Upcoming (and recently passed) key dates across the documents this person can see."""
    import datetime as dt

    from ..models import KeyDate

    today = timezone.localdate()
    visible = Packet.objects.visible_to(request.membership).filter(trashed_at__isnull=True)
    qs = KeyDate.objects.filter(workspace=request.workspace, packet__in=visible).select_related("packet", "packet__workflow")
    if request.method == "POST":
        k = get_object_or_404(qs, pk=request.POST.get("done") or 0)
        k.done_at = None if k.done_at else timezone.now()
        k.save(update_fields=["done_at"])
        engine.audit(request.workspace, "keydate.done" if k.done_at else "keydate.reopened", k.label, actor=request.user, packet=k.packet, request=request)
        return redirect(request.get_full_path())
    show = request.GET.get("show", "upcoming")
    window = {"upcoming": (today, today + dt.timedelta(days=365)), "passed": (today - dt.timedelta(days=90), today - dt.timedelta(days=1))}
    lo, hi = window.get(show, window["upcoming"])
    rows = list(qs.filter(date__gte=lo, date__lte=hi).order_by("date" if show != "passed" else "-date")[:300])
    for k in rows:
        k.days_left = (k.date - today).days
    return render(request, "flows/key_dates.html", {
        "rows": rows, "show": show, "nav": "documents",
        "soon": qs.filter(done_at__isnull=True, date__gte=today, date__lte=today + dt.timedelta(days=30)).count()})


@login_required
def ask_documents(request):
    """Ask your documents: find documents by meaning and answer with citations."""
    from .. import ai_actions, semantic

    q = (request.GET.get("q") or "").strip()[:300]
    result, engine = None, ""
    if q:
        result = semantic.answer(request.membership, q, user=request.user)
        cites = semantic.cited(result["answer"])
        for h in result["hits"]:
            h["cited"] = h["packet"].pk in cites
        engine = semantic.embed_engine(request.workspace)
        engine_label = engine.split(":", 1)[-1] if engine else ""
    return render(request, "flows/ask_documents.html", {
        "q": q, "result": result, "nav": "documents", "ai_on": ai_actions.enabled(request.workspace),
        "search_model": engine_label if q else "",
        "examples": ["claims over 10,000 still waiting for a decision", "documents that mention water damage",
                     "vendor contracts that renew this year", "anything declined last month and why"]})
