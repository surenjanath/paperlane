"""Documents (packets): list, detail/status, revisions, comments, audit, bots log, downloads and actions."""
import io
import zipfile

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from .. import privacy
from .. import engine, mailer, services
from ..exports import audit_csv, metadata_csv, packet_pdf, rendered_documents
from ..models import Comment, Packet, StepRun, Tag, Workflow, WorkflowDocument
from ..permissions import get_packet, get_workflow, require
from core.utils import safe_next

from flows.queries_util import RUNS_PREFETCH  # noqa: E402


def attach_next_due(packets):
    """Set next_due on a page of documents with one query (instead of one per row)."""
    from django.db.models import Min

    packets = list(packets)
    due = dict(StepRun.objects.filter(packet__in=packets, status__in=["pending", "opened"], due_at__isnull=False)
               .values("packet_id").annotate(d=Min("due_at")).values_list("packet_id", "d"))
    for p in packets:
        p.next_due = due.get(p.pk) if p.status == Packet.IN_PROGRESS else None
    return packets


def filtered_packets(request, base=None, query=None):
    qs = base if base is not None else Packet.objects.visible_to(request.membership).live().filter(is_test=False)
    g = request.GET if query is None else query
    quick = g.get("quick")
    if quick == "attention":
        qs = qs.filter(Q(status=Packet.DECLINED) | Q(runs__status__in=["expired", "declined"])).distinct()
    elif quick == "stale":
        qs = qs.filter(status=Packet.IN_PROGRESS, last_activity_at__lt=timezone.now() - timezone.timedelta(days=7))
    elif quick == "mine":
        qs = qs.filter(created_by=request.user)
    elif quick == "overdue":
        qs = qs.filter(status=Packet.IN_PROGRESS, runs__status__in=["pending", "opened"],
                       runs__due_at__lte=timezone.now()).distinct()
    if g.get("status"):
        qs = qs.filter(status=g["status"])
    if g.get("workflow"):
        qs = qs.filter(workflow_id=g["workflow"])
    if g.get("tag"):
        qs = qs.filter(tags__name=g["tag"])
    if g.get("from"):
        qs = qs.filter(created_at__date__gte=g["from"])
    if g.get("to"):
        qs = qs.filter(created_at__date__lte=g["to"])
    if g.get("q"):
        q = g["q"].strip()
        qs = qs.filter(Q(name__icontains=q) | Q(workflow__name__icontains=q) | Q(tags__name__icontains=q)
                       | Q(runs__assignee_email__icontains=q)).distinct()
    sort = g.get("sort", "activity")
    return qs.order_by({"activity_asc": "last_activity_at", "created": "-created_at", "created_asc": "created_at",
                        "name": "name"}.get(sort, "-last_activity_at"))


@login_required
def documents(request):
    qs = filtered_packets(request)
    page = Paginator(qs.select_related("workflow", "created_by").prefetch_related("tags", RUNS_PREFETCH(), "revisions"), 15).get_page(request.GET.get("page"))
    page.object_list = attach_next_due(page.object_list)
    total = Packet.objects.visible_to(request.membership).live().filter(is_test=False).count()
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "flows/documents.html", {
        "page": page, "total": total, "nav": "documents", "querystring": params.urlencode(),
        "workflows": Workflow.objects.visible_to(request.membership).exclude(kind="document").order_by("name"),
        "tags": Tag.objects.filter(workspace=request.workspace), "statuses": Packet.STATUSES, "g": request.GET,
        "sort_options": SORT_OPTIONS, "ai_search": _ai_search_on(request.workspace), "saved_views": _saved_views(request),
        "has_filters": any(k in request.GET for k in VIEW_KEYS),
    })


VIEW_KEYS = ("q", "status", "workflow", "tag", "from", "to", "quick", "sort")


def _clean_query(get):
    from urllib.parse import urlencode

    return urlencode([(k, get[k]) for k in VIEW_KEYS if get.get(k)])


def _saved_views(request):
    from django.http import QueryDict

    from ..models import SavedView

    current = _clean_query(request.GET)
    out = []
    for v in SavedView.objects.filter(workspace=request.workspace).filter(Q(owner=request.user) | Q(shared=True))[:12]:
        out.append({"v": v, "count": filtered_packets(request, query=QueryDict(v.query)).count(), "on": v.query == current,
                    "mine": v.owner_id == request.user.id})
    return out


@login_required
@require_POST
def saved_view(request, pk=None):
    from urllib.parse import urlsplit

    from django.http import QueryDict

    from ..models import SavedView

    if pk:
        v = get_object_or_404(SavedView, workspace=request.workspace, pk=pk)
        if v.owner_id != request.user.id and not request.membership.can_manage:
            raise PermissionDenied
        v.delete()
        messages.success(request, f"View “{v.name}” removed.")
        return redirect("flows:documents")
    query = _clean_query(QueryDict(urlsplit(request.POST.get("query", "")).query or request.POST.get("query", "").lstrip("?")))
    name = request.POST.get("name", "").strip()[:80]
    if not name or not query:
        messages.error(request, "Give the view a name and set some filters first.")
        return redirect("flows:documents")
    SavedView.objects.create(workspace=request.workspace, owner=request.user, name=name, query=query,
                             shared=request.POST.get("shared") == "on" and request.membership.can_create)
    messages.success(request, f"View “{name}” saved.")
    return redirect(reverse("flows:documents") + "?" + query)


def _ai_search_on(ws):
    from .. import ai_actions, ai_providers

    return ai_actions.enabled(ws, "nl_search") and (bool(ai_actions.settings_for(ws).get("engine"))
                                                     or ai_providers.default_engine() != ai_providers.PLANNER)


SORT_OPTIONS = [("activity", "Last activity: newest first"), ("activity_asc", "Last activity: oldest first"),
                ("created", "Date created: newest first"), ("created_asc", "Date created: oldest first"), ("name", "Name: A–Z")]


@login_required
def documents_export_all(request):
    resp = HttpResponse(metadata_csv(filtered_packets(request).select_related("workflow").prefetch_related("tags")[:5000],
                                     can_see=lambda p: privacy.can_see(request.user, request.membership, p)),
                        content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="documents-metadata.csv"'
    return resp


@login_required
@require_POST
def documents_bulk(request):
    ids = request.POST.getlist("ids")
    action = request.POST.get("action")
    packets = Packet.objects.visible_to(request.membership).filter(pk__in=ids)
    if action == "export":
        resp = HttpResponse(metadata_csv(packets.prefetch_related("tags"), can_see=lambda p: privacy.can_see(request.user, request.membership, p)), content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="documents-metadata.csv"'
        return resp
    if action == "download":
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in packets:
                z.writestr(f"{p.pk}-{slugify(p.name)[:60]}.pdf", packet_pdf(p) or b"")
        resp = HttpResponse(buf.getvalue(), content_type="application/zip")
        resp["Content-Disposition"] = 'attachment; filename="documents.zip"'
        return resp
    manageable = [p for p in packets if _can_manage(request, p)]
    now = timezone.now()
    for p in manageable:
        if action == "archive":
            p.archived_at = now
        elif action == "trash":
            p.trashed_at = now
        elif action == "tags":
            for n in [t.strip() for t in request.POST.get("tags", "").split(",") if t.strip()]:
                p.tags.add(Tag.objects.get_or_create(workspace=request.workspace, name=n[:80])[0])
        elif action == "lock":
            p.locked = True
        p.save()
        engine.audit(request.workspace, f"packet.{action}", "bulk action", actor=request.user, packet=p, request=request)
    messages.success(request, f"{len(manageable)} document(s) updated.")
    return safe_next(request, "flows:documents")


def _can_manage(request, packet):
    try:
        get_packet(request, packet.pk, manage=True)
        return True
    except PermissionDenied:
        return False


_PACKET_TABS = {
    "status": "flows/packet/status.html",
    "data": "flows/packet/documents.html",
    "revisions": "flows/packet/revisions.html",
    "comments": "flows/packet/comments.html",
    "ai": "flows/packet/ask.html",
    "files": "flows/packet/files.html",
    "history": "flows/packet/history.html",
    "audit": "flows/packet/audit.html",
    "bots": "flows/packet/bots.html",
    "emails": "flows/packet/emails.html",
    "path": "flows/packet/path.html",
}


def _span(seconds):
    h = seconds / 3600
    return f"{max(1, round(h * 60))} min" if h < 1 else (f"{h:.1f} h" if h < 24 else f"{h / 24:.1f} days")


def _path_context(packet):
    """The workflow diagram with this document's route on it: what was done (by whom, how long), where it is now,
    what it skipped and what is still ahead."""
    from django.utils import timezone

    from .builder import canvas_context

    ctx = canvas_context(packet.workflow)
    states = {d["step"]["key"]: d for d in _status_diagram(packet)}
    now = timezone.now()
    for st in ctx["steps"]:
        d = states.get(st.key)
        st.path_state = d["state"] if d else "not_in_version"
        runs = d["runs"] if d else []
        info = ""
        done = [r for r in runs if r.status == "completed" and r.completed_at]
        if st.path_state == "done" and done:
            r = done[-1]
            info = f"{r.assignee_name or r.assignee_email or 'Done'} · {_span((r.completed_at - r.created_at).total_seconds())}"
        elif st.path_state == "current":
            r = next(r for r in runs if r.status in ("pending", "opened"))
            who = r.assignee_name or r.assignee_email or (r.assignee_group.name if r.assignee_group_id else "")
            info = f"Waiting for {who or 'someone'} · {_span((now - r.created_at).total_seconds())}"
            if r.due_at and r.due_at < now:
                info += " · overdue"
        elif st.path_state == "declined":
            r = next((r for r in runs if r.status == "declined"), None)
            info = f"Declined{(' by ' + (r.assignee_name or r.assignee_email)) if r and (r.assignee_name or r.assignee_email) else ''}"
        if st.path_state == "not_in_version":
            info = "Not in the version this document runs on"
        st.path_info = info
    ctx.update(readonly=True, path_old_version=packet.version != packet.workflow.version
               or any(x.path_state == "not_in_version" for x in ctx["steps"]),
               path_counts={k: sum(1 for x in ctx["steps"] if x.path_state == k) for k in ("done", "current", "skipped", "upcoming")})
    return ctx


def _status_diagram(packet):
    runs = list(packet.runs.all())
    by_step = {}
    for r in runs:
        by_step.setdefault(r.step_key, []).append(r)
    diagram = []
    for s in packet.steps:
        state = "upcoming"
        rs = by_step.get(s["key"], [])
        if any(r.status in ("pending", "opened") for r in rs):
            state = "current"
        elif any(r.status == "completed" for r in rs):
            state = "done"
        elif any(r.status == "declined" for r in rs):
            state = "declined"
        elif s["key"] in (packet.visited or []):
            state = "done" if s["kind"] != "recipient" else "skipped"
        elif rs and all(r.status in ("skipped", "revoked", "expired") for r in rs):
            state = rs[-1].status
        if state == "upcoming" and packet.status in (Packet.COMPLETED, Packet.DECLINED, Packet.CANCELLED):
            state = "not_used"  # finished on another path (other branch lane / jump)
        diagram.append({"step": s, "state": state, "runs": rs})
    return diagram


@login_required
def packet_detail(request, pk):
    """One tab per template. Only that tab's data is loaded."""
    packet = get_packet(request, pk)
    from .. import ai_actions, ai_providers

    ai_cfg = ai_actions.settings_for(packet.workspace)
    ask_ai = packet.workspace.feature("ai") and not ai_cfg.get("disabled") and ai_cfg.get("ask_ai", True)
    tab = request.GET.get("tab", "status")
    if tab not in _PACKET_TABS or (tab == "ai" and not ask_ai):
        tab = "status"
    ctx = {"packet": packet, "tab": tab, "nav": "documents", "ask_ai": ask_ai,
           "can_manage": _can_manage(request, packet), "tab_template": _PACKET_TABS[tab]}
    if tab == "status":
        from .. import diagnose as dg

        ctx["diagram"] = _status_diagram(packet)
        ctx["recipient_steps"] = [s for s in packet.steps if s["kind"] == "recipient"]
        ctx["seal"] = packet.seals.filter(superseded_at__isnull=True).first()
        if ctx["seal"] and request.GET.get("check") == "1":
            from .. import sealing

            ctx["seal_check"] = sealing.check(ctx["seal"])
        health = dg.diagnose(packet)
        ctx["health"] = health if dg.RANK[health["severity"]] >= dg.RANK["medium"] else None
        ctx["watchdog"] = [w for w in (packet.flags or {}).get("watchdog") or [] if not w.get("dismissed")]
    elif tab == "data":
        ctx["documents"] = rendered_documents(packet, mask=not privacy.can_see(request.user, request.membership, packet))
    elif tab == "revisions":
        ctx["revisions"] = packet.revisions.all()
    elif tab == "audit":
        ctx["events"] = packet.audit_events.all()
    elif tab == "bots":
        ctx["logs"] = packet.bot_logs.all()
    elif tab == "comments":
        ctx["comments"] = packet.comments.select_related("author")
        from ..mentions import members

        ctx["mention_people"] = [{"name": u.get_full_name() or u.username, "email": u.email,
                                  "token": f'"{u.get_full_name()}"' if u.get_full_name() else u.username} for u in members(packet.workspace)]
    elif tab == "history":
        qs = packet.field_changes.all()
        ctx["history_fields"] = sorted(set(qs.values_list("field_id", "label")), key=lambda x: x[1].lower())
        if request.GET.get("field"):
            qs = qs.filter(field_id=request.GET["field"])
        ctx["changes"] = list(qs[:300])
    elif tab == "files":
        ctx["files"] = packet.files.all()
    elif tab == "emails":
        ctx["emails"] = packet.outboundemail_set.all()
    elif tab == "path":
        ctx.update({k: v for k, v in _path_context(packet).items() if k not in ("wf",)}, wf=packet.workflow)
    elif tab == "ai":
        ctx["ai_label"] = ai_providers.label(ai_cfg["engine"]) if ai_cfg.get("engine") else "automatic"
        ctx["ai_prompts"] = ["Summarize this document", "What is still missing?", "Anything unusual or risky?",
                             "Draft a short update for the client"]
    return render(request, "flows/packet_detail.html", ctx)


@login_required
@require_POST
def packet_action(request, pk, action):
    packet = get_packet(request, pk, manage=action not in ("comment",))
    p = request.POST
    from core.utils import safe_url

    back = safe_url(request, p.get("next"), reverse("flows:packet_detail", args=[packet.pk]))
    now = timezone.now()
    if packet.locked and action in ("revise", "rename"):
        messages.error(request, "This document is locked. Unlock it first.")
        return redirect(back)
    if action == "watchdog_dismiss":
        from ..watchdog import dismiss

        dismiss(packet, p.get("finding", ""))
        engine.audit(packet.workspace, "watchdog.dismissed", p.get("finding", ""), actor=request.user, packet=packet, request=request)
        return redirect(back)
    if action == "lock":
        packet.locked = not packet.locked
        packet.save(update_fields=["locked"])
        messages.success(request, "Document locked." if packet.locked else "Document unlocked.")
    elif action == "hold":
        on = not (packet.flags or {}).get("legal_hold")
        packet.flags = {**(packet.flags or {}), "legal_hold": on}
        packet.save(update_fields=["flags"])
        messages.success(request, "Legal hold on — retention rules will never delete this document." if on else "Legal hold removed.")
    elif action == "tags":
        names = [t.strip() for t in p.get("tags", "").split(",") if t.strip()]
        packet.tags.set([Tag.objects.get_or_create(workspace=request.workspace, name=n[:80])[0] for n in names])
    elif action == "rename":
        packet.name = (p.get("name") or packet.name)[:255]
        packet.save(update_fields=["name"])
    elif action == "archive":
        packet.archived_at = now
        packet.save(update_fields=["archived_at"])
        messages.success(request, "Document archived.")
        back = reverse("flows:documents")
    elif action == "trash":
        packet.trashed_at = now
        packet.save(update_fields=["trashed_at"])
        messages.success(request, "Document moved to Trash.")
        back = reverse("flows:documents")
    elif action == "restore":
        packet.trashed_at = None
        packet.archived_at = None
        packet.save(update_fields=["trashed_at", "archived_at"])
        messages.success(request, "Document restored.")
    elif action == "delete":
        if not packet.trashed_at:
            raise PermissionDenied("Move the document to Trash first.")
        packet.delete()
        messages.success(request, "Document deleted permanently.")
        return safe_next(request, "core:trash")
    elif action == "cancel":
        engine.cancel_packet(packet, user=request.user, request=request)
        messages.success(request, "Document cancelled.")
    elif action == "revise":
        engine.revise(packet, p.get("step_key"), user=request.user, request=request)
        messages.success(request, "The document was sent back for revision.")
    elif action in ("resend", "remind"):
        run = get_object_or_404(StepRun, packet=packet, pk=p.get("run"))
        if run.assignee_email:
            mailer.send(packet.workspace, run.assignee_email, "invite" if action == "resend" else "reminder", {
                "user_name": run.assignee_name or run.assignee_email, "workflow_name": packet.workflow.name,
                "document_name": packet.name, "link": run.absolute_fill_url(), "message": ""}, packet=packet)
            run.reminders_sent += action == "remind"
            run.last_reminder_at = now if action == "remind" else run.last_reminder_at
            run.save()
            messages.success(request, f"Email sent to {run.assignee_email}.")
        else:
            messages.error(request, "This step has no email recipient — copy its link instead.")
        engine.audit(request.workspace, f"step.{action}", run.assignee_email, actor=request.user, packet=packet, request=request)
    elif action == "reassign":
        run = get_object_or_404(StepRun, packet=packet, pk=p.get("run"), status__in=["pending", "opened"])
        email = p.get("email", "").strip()
        if email:
            run.assignee_email = email
            run.assignee_name = p.get("name", "").strip() or email
            run.assignee_user = get_user_model().objects.filter(email__iexact=email).first()
            run.assignee_group = None
            run.claimed_by, run.claimed_at = None, None
            run.save()
            mailer.send(packet.workspace, email, "invite", {"user_name": run.assignee_name, "workflow_name": packet.workflow.name,
                                                            "document_name": packet.name, "link": run.absolute_fill_url(), "message": ""},
                        packet=packet)
            if run.assignee_user:
                engine.notify(run.assignee_user, packet.workspace, f"{packet.name} is waiting for you", run.step_name,
                              run.fill_url(), kind="assigned")
            engine.audit(request.workspace, "step.reassigned", f"{run.step_name} → {email}", actor=request.user, packet=packet, request=request)
            messages.success(request, f"Step reassigned to {email}.")
    elif action == "comment":
        text = p.get("text", "").strip()
        if text:
            Comment.objects.create(packet=packet, author=request.user, author_label=engine.actor_label(request.user), text=text)
            engine.audit(request.workspace, "packet.comment", text[:200], actor=request.user, packet=packet, request=request)
            from ..mentions import notify as notify_mentions

            link = reverse("flows:packet_detail", args=[packet.pk]) + "?tab=comments"
            told = notify_mentions(packet, request.user, text, link)
            if told:
                messages.success(request, "Comment posted — notified " + ", ".join(u.get_full_name() or u.email for u in told) + ".")
        back = reverse("flows:packet_detail", args=[packet.pk]) + "?tab=comments"
    elif action == "save_and_send":
        to = p.get("email", "").strip()
        pdf = packet_pdf(packet)
        ok = mailer.send(packet.workspace, to, "custom", {"subject": f"Documents: {packet.name}",
                                                          "message": p.get("message") or f"Please find {packet.name} attached."},
                         packet=packet, attachments=[(f"{slugify(packet.name)}.pdf", pdf, "application/pdf")] if pdf else None)
        engine.audit(request.workspace, "packet.sent_copy", to, actor=request.user, packet=packet, request=request)
        messages.success(request, f"A copy was sent to {to}.") if ok else messages.error(request, "Enter a valid email address.")
    else:
        raise PermissionDenied("Unknown action")
    if action not in ("comment",):
        engine.audit(request.workspace, f"packet.{action}", "", actor=request.user, packet=packet, request=request)
    return redirect(back)


@login_required
def packet_download(request, pk):
    packet = get_packet(request, pk)
    rev = request.GET.get("revision")
    data = None
    if rev:
        data = get_object_or_404(packet.revisions, number=rev).data
    if request.GET.get("format") == "html":
        return render(request, "flows/print_packet.html", {"packet": packet, "documents": rendered_documents(packet, data, mask=not privacy.can_see(request.user, request.membership, packet)),
                                                           "events": packet.audit_events.order_by("created_at"),
                                                           "certificate": True, "printable": True})
    see = privacy.can_see(request.user, request.membership, packet)
    if not see and privacy.sensitive_ids(packet.documents):  # a masked copy instead of the sealed original
        pdf = packet_pdf(packet, data, mask=True)
        if pdf is None:
            return redirect(reverse("flows:packet_download", args=[pk]) + "?format=html")
        engine.audit(request.workspace, "packet.downloaded", "masked copy", actor=request.user, packet=packet, request=request)
        resp = HttpResponse(pdf, content_type="application/pdf")
        resp["Content-Disposition"] = f'attachment; filename="{slugify(packet.name)[:80] or packet.pk}-masked.pdf"'
        return resp
    sealed = packet.seals.filter(superseded_at__isnull=True).first() if not rev and packet.status == "completed" else None
    if sealed and sealed.pdf:  # the frozen, sealed PDF — byte-for-byte what /verify/ recognises
        engine.audit(request.workspace, "packet.downloaded", f"sealed copy {sealed.code}", actor=request.user, packet=packet, request=request)
        resp = HttpResponse(sealed.pdf.open("rb").read(), content_type="application/pdf")
        resp["Content-Disposition"] = f'attachment; filename="{slugify(packet.name)[:80] or packet.pk}.pdf"'
        return resp
    pdf = packet_pdf(packet, data)
    if pdf is None:
        return redirect(reverse("flows:packet_download", args=[pk]) + "?format=html")
    engine.audit(request.workspace, "packet.downloaded", f"revision {rev}" if rev else "", actor=request.user, packet=packet, request=request)
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{slugify(packet.name)[:80] or packet.pk}.pdf"'
    return resp


@login_required
def packet_revision(request, pk, number):
    packet = get_packet(request, pk)
    revision = get_object_or_404(packet.revisions, number=number)
    return render(request, "flows/revision.html", {"packet": packet, "revision": revision, "nav": "documents",
                                                   "documents": rendered_documents(packet, revision.data, mask=not privacy.can_see(request.user, request.membership, packet))})


@login_required
def packet_export(request, pk):
    packet = get_packet(request, pk)
    kind = request.GET.get("kind", "metadata")
    if kind == "audit":
        body, name = audit_csv(packet.audit_events.order_by("created_at")), f"audit-{packet.pk}.csv"
    else:
        body, name = metadata_csv([packet], can_see=lambda p: privacy.can_see(request.user, request.membership, p)), f"metadata-{packet.pk}.csv"
    resp = HttpResponse(body, content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp


# ---- Create > Document (one-time request) -----------------------------------------------------
@login_required
@require("create")
def document_new(request):
    if request.method == "POST":
        name = request.POST.get("name") or "Untitled document"
        wf = services.create_workflow(request.workspace, request.user, name=name, kind=Workflow.DOCUMENT, with_document=False)
        first = wf.steps.first()
        emails = [e.strip() for e in request.POST.getlist("emails") if e.strip()]
        names = request.POST.getlist("names")
        for i, email in enumerate(emails or [request.user.email]):
            step = first if i == 0 else first.__class__(workflow=wf, order=(i + 1) * 10)
            step.name = (names[i] if i < len(names) and names[i] else f"Recipient {i + 1}")[:120]
            step.recipient_email = email
            step.message_body = request.POST.get("message") or step.message_body
            step.save()
        upload = request.FILES.get("file")
        if upload:
            WorkflowDocument.objects.create(
                workflow=wf, name=upload.name.rsplit(".", 1)[0][:200], kind=WorkflowDocument.FILE, file=upload,
                schema={"fields": [{"id": services.new_key(), "type": "signature", "label": "Signature", "name": "Signature",
                                    "required": True, "role": "anyone"}]})
        else:
            WorkflowDocument.objects.create(workflow=wf, name=name, schema=services.blank_form_schema())
        doc = wf.documents.first()
        return redirect("flows:editor", wf.pk, doc.pk)
    return render(request, "flows/document_new.html", {"nav": "documents"})


@login_required
@require("create")
def document_send(request, pk):
    wf = get_workflow(request, pk, edit=True)
    if request.method == "POST":
        services.publish(wf, request.user)
        packet = engine.start_packet(wf, user=request.user, source="manual", name=wf.name, request=request)
        messages.success(request, f"“{packet.name}” was sent.")
        run = packet.runs.filter(status="pending").first()
        if run and run.can_act(request.user):
            return redirect("flows:fill", run.token)
        return redirect("flows:packet_detail", packet.pk)
    return render(request, "flows/document_send.html", {"wf": wf, "steps": wf.ordered_steps(), "nav": "documents"})
