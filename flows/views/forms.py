"""Start forms: library, builder (reuses the document editor), settings, in-form bots, submissions and the
public form page that starts a workflow."""
import copy
import csv
import io
import json
import re
import secrets

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .. import engine, intake
from ..conditions import evaluate
from ..fieldtypes import COMPOSITES, CONDITION_OPERATORS, ELEMENTS, WIDGETS, field_value_to_text
from ..form_templates import TEMPLATES
from ..models import DataSheet, FormFile, FormSubmission, IntakeForm, Workflow
from .fill import build_doc_view, parse_values, validate
from core.utils import safe_next


def _js(v):
    return json.dumps(v).replace("</", "<\\/")


def _get_form(request, pk, edit=False):
    form = get_object_or_404(IntakeForm, pk=pk, workspace=request.workspace)
    if edit and not request.membership.can_create:
        raise PermissionDenied("Your role cannot edit forms.")
    return form


def _workflows(request):
    return Workflow.objects.visible_to(request.membership).live().filter(kind="workflow").order_by("name")


# ---- library -------------------------------------------------------------------------------
@login_required
def forms_list(request):
    qs = IntakeForm.objects.filter(workspace=request.workspace, archived_at__isnull=True).select_related("workflow")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(name__icontains=q)
    forms = list(qs)
    for f in forms:
        f.n_subs = f.submissions.count()
        f.link = engine.absolute(f.public_url(), request)
    return render(request, "flows/forms/list.html", {"forms": forms, "q": q, "nav": "forms", "templates": TEMPLATES})


@login_required
@require_POST
def forms_bulk(request):
    """Publish, archive, or delete the forms checked on the Forms page."""
    if not request.membership or not request.membership.can_create:
        raise PermissionDenied
    action = request.POST.get("action")
    if action not in ("publish", "unpublish", "archive", "delete"):
        raise PermissionDenied("Unknown action")
    chosen = list(IntakeForm.objects.filter(workspace=request.workspace, pk__in=request.POST.getlist("ids"), archived_at__isnull=True))
    n = skipped = 0
    now = timezone.now()
    for form in chosen:
        if action == "publish":
            if not form.input_fields():
                skipped += 1
                continue
            form.status = IntakeForm.PUBLISHED
            form.save(update_fields=["status"])
        elif action == "unpublish":
            form.status = IntakeForm.DRAFT
            form.save(update_fields=["status"])
        elif action == "archive":
            form.archived_at = now
            form.save(update_fields=["archived_at"])
        else:
            form.delete()
        engine.audit(request.workspace, f"form.{action}", form.name, actor=request.user, request=request)
        n += 1
    if action == "publish" and skipped:
        messages.success(request, f"{n} form{'' if n == 1 else 's'} published. {skipped} still need fields.")
    elif n:
        done = {"publish": "published", "unpublish": "unpublished", "archive": "archived", "delete": "deleted"}[action]
        messages.success(request, f"{n} form{'' if n == 1 else 's'} {done}.")
    else:
        messages.success(request, "No forms were changed.")
    from core.utils import safe_url
    return redirect(safe_url(request, request.META.get("HTTP_REFERER"), reverse("flows:forms")))


@login_required
def form_new(request):
    if not request.membership.can_create:
        raise PermissionDenied("Your role cannot create forms.")
    if request.method == "POST":
        key = request.POST.get("template", "")
        tpl = TEMPLATES.get(key)
        wf = _workflows(request).filter(pk=request.POST.get("workflow") or 0).first()
        if tpl and wf is None and tpl.get("workflow_hint"):
            wf = _workflows(request).filter(name=tpl["workflow_hint"]).first()
        name = (request.POST.get("name") or (tpl["name"] if tpl else "Untitled form")).strip()[:200]
        form = IntakeForm.objects.create(
            workspace=request.workspace, workflow=wf, name=name, created_by=request.user, template_key=key if tpl else "",
            description=tpl["description"] if tpl else "",
            schema={"fields": copy.deepcopy(tpl["fields"])} if tpl else {"fields": [
                {"id": "h1", "type": "heading", "label": name, "text": "", "role": "anyone"},
                {"id": "email", "type": "email", "label": "Email", "name": "Email", "required": True, "role": "anyone"}]},
            lookups=copy.deepcopy(tpl["lookups"]) if tpl else [],
            options=copy.deepcopy(tpl["options"]) if tpl else {"submit_label": "Submit", "recipient_email_field": "email"})
        if tpl:
            from ..form_templates import ensure_lookup_sheets

            ensure_lookup_sheets(request.workspace, tpl["lookups"])
        engine.audit(request.workspace, "form.created", f"Form “{form.name}” created", actor=request.user, request=request)
        messages.success(request, f"Form “{form.name}” created." + (" Design its fields, then publish it." if not tpl else ""))
        return redirect("flows:form_edit", form.pk) if not tpl else redirect("flows:form_settings", form.pk)
    return render(request, "flows/forms/new_modal.html", {
        "templates": TEMPLATES, "workflows": _workflows(request), "preselect": request.GET.get("workflow", "")})


# ---- builder (document editor) ----------------------------------------------------------------
@login_required
def form_edit(request, pk):
    form = _get_form(request, pk, edit=True)
    ed = {"kind": "Form", "name": form.name, "hide_roles": True,
          "back_url": reverse("flows:form_settings", args=[form.pk]),
          "save_url": reverse("flows:form_save", args=[form.pk]),
          "continue_url": reverse("flows:form_settings", args=[form.pk]),
          "preview_url": reverse("flows:form_public", args=[form.token]) + "?preview=1",
          "continue_label": "Continue to settings", "ai_url": reverse("flows:form_ai", args=[form.pk])}
    return render(request, "flows/editor.html", {
        "ed": ed, "elements": ELEMENTS, "widgets": WIDGETS, "schema_json": _js(form.schema or {"fields": []}),
        "design_json": _js(form.design or (request.workspace.theme or {}).get("default_form_design") or {}),
        "roles_json": _js([]), "composites_json": _js(COMPOSITES), "ops_json": _js(CONDITION_OPERATORS)})


@login_required
@require_POST
def form_ai(request, pk):
    """AI builder for start forms (everyone fills every field, so no roles)."""
    from .editor import run_editor_ai

    form = _get_form(request, pk, edit=True)
    return run_editor_ai(request, form.workspace)


@login_required
@require_POST
def form_save(request, pk):
    form = _get_form(request, pk, edit=True)
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)
    fields = (payload.get("schema") or {}).get("fields")
    if not isinstance(fields, list):
        return JsonResponse({"ok": False, "error": "schema.fields must be a list"}, status=400)
    seen = set()
    for f in fields:
        if not isinstance(f, dict) or not f.get("id") or not f.get("type") or f["id"] in seen:
            return JsonResponse({"ok": False, "error": "Every field needs a unique id and a type"}, status=400)
        seen.add(f["id"])
        f["role"] = "anyone"  # everyone filling the form can edit every field
    form.schema = {"fields": fields}
    form.design = payload.get("design") or {}
    if payload.get("name"):
        form.name = str(payload["name"])[:200]
    form.save()
    return JsonResponse({"ok": True})


# ---- settings, in-form bots and mapping -------------------------------------------------------
@login_required
def form_settings(request, pk):
    form = _get_form(request, pk)
    can_edit = request.membership.can_create
    if request.method == "POST":
        if not can_edit:
            raise PermissionDenied
        p = request.POST
        section = p.get("section", "general")
        opts = dict(form.options or {})
        if section == "general":
            form.name = (p.get("name") or form.name)[:200]
            form.description = p.get("description", "")[:400]
            form.workflow = _workflows(request).filter(pk=p.get("workflow") or 0).first()
            for k in ("submit_label", "success_title", "success_body", "redirect_url", "recipient_email_field",
                      "recipient_name_field", "sheet", "notify_emails", "document_name", "closed_message"):
                opts[k] = p.get(k, "").strip()
            for k in ("complete_first_step", "require_login", "closed"):
                opts[k] = p.get(k) == "on"
        elif section == "mapping":
            opts["mapping"] = {k[4:]: v for k, v in p.items() if k.startswith("map:") and v}
        elif section == "lookups":
            form.lookups = _lookups_from_post(p, form)
        elif section == "payment":
            cur = (p.get("currency") or "usd").strip().lower()[:3]
            opts["payment"] = {"enabled": p.get("enabled") == "on", "amount": p.get("amount", "").strip()[:20],
                               "amount_field": p.get("amount_field", "").strip()[:120], "currency": cur if re.fullmatch(r"[a-z]{3}", cur) else "usd",
                               "description": p.get("description_pay", "").strip()[:120]}
        form.options = opts
        form.save()
        messages.success(request, "Form settings saved.")
        return redirect(reverse("flows:form_settings", args=[form.pk]) + f"#{section}")
    fields = [f for f in form.input_fields()]
    return render(request, "flows/forms/settings.html", {
        "form": form, "o": form.options or {}, "fields": fields, "all_fields": form.fields, "nav": "forms",
        "workflows": _workflows(request), "can_edit": can_edit,
        "public_link": engine.absolute(form.public_url(), request),
        "embed": f'<iframe src="{engine.absolute(form.public_url(), request)}?embed=1" style="width:100%;min-height:900px;border:0" title="{form.name}"></iframe>',
        "match": intake.match_report(form), "sources": intake.LOOKUP_SOURCES,
        "connectors": __import__("flows.models", fromlist=["Connector"]).Connector.objects.filter(workspace=form.workspace, active=True),
        "sheets": DataSheet.objects.filter(workspace=request.workspace),
        "lookups": [dict(lk, idx=i) for i, lk in enumerate(form.lookups or [])],
        "email_fields": [f for f in fields if f.get("type") in ("email", "text")],
        "n_subs": form.submissions.count(),
        "payments_configured": __import__("flows.payments", fromlist=["configured"]).configured(),
        "pay": (form.options or {}).get("payment") or {},
        "amount_fields": [f for f in fields if f.get("type") in ("number", "currency", "formula")],
    })


def _lookups_from_post(p, form):
    out = []
    ids = {f["id"] for f in form.fields}
    for i in range(int(p.get("lookup_count", 0) or 0)):
        if p.get(f"lk{i}_delete") == "on":
            continue
        froms, tos = p.getlist(f"lk{i}_from"), p.getlist(f"lk{i}_to")
        mapping = [{"from": a.strip(), "to": b} for a, b in zip(froms, tos) if a.strip() and b in ids]
        try:
            min_len = max(1, int(p.get(f"lk{i}_min_length") or 1))
        except ValueError:
            min_len = 1
        out.append({
            "id": p.get(f"lk{i}_id") or secrets.token_hex(4), "name": p.get(f"lk{i}_name", "").strip()[:80] or "Lookup",
            "trigger": p.get(f"lk{i}_trigger", ""), "source": p.get(f"lk{i}_source", "sheet"),
            "sheet": p.get(f"lk{i}_sheet", ""), "key_column": p.get(f"lk{i}_key_column", "").strip(),
            "workflow_id": int(p.get(f"lk{i}_workflow_id") or 0) or None, "key_field": p.get(f"lk{i}_key_field", "").strip(),
            "url": p.get(f"lk{i}_url", "").strip(), "method": p.get(f"lk{i}_method", "GET"),
            "auth_header": p.get(f"lk{i}_auth_header", "").strip(), "min_length": min_len,
            "connector": int(p.get(f"lk{i}_connector") or 0) or None, "action": p.get(f"lk{i}_action", "").strip(),
            "found_message": p.get(f"lk{i}_found", "").strip(), "not_found_message": p.get(f"lk{i}_notfound", "").strip(),
            "map": mapping,
        })
    if p.get("add_lookup") == "1":
        first = next((f["id"] for f in form.input_fields()), "")
        out.append({"id": secrets.token_hex(4), "name": "New lookup", "trigger": first, "source": "sheet", "min_length": 3,
                    "map": []})
    return out


@login_required
@require_POST
def form_action(request, pk, action):
    form = _get_form(request, pk, edit=True)
    if action == "publish":
        if not form.input_fields():
            messages.error(request, "Add at least one field before publishing.")
        else:
            form.status = IntakeForm.PUBLISHED
            messages.success(request, "Form published — the public link is live.")
    elif action == "unpublish":
        form.status = IntakeForm.DRAFT
        messages.success(request, "Form unpublished — the public link is closed.")
    elif action == "duplicate":
        clone = IntakeForm.objects.create(
            workspace=form.workspace, workflow=form.workflow, name=f"{form.name} (copy)", description=form.description,
            schema=copy.deepcopy(form.schema), design=copy.deepcopy(form.design), options=copy.deepcopy(form.options),
            lookups=copy.deepcopy(form.lookups), created_by=request.user, template_key=form.template_key)
        messages.success(request, f"Duplicated as “{clone.name}”.")
        return redirect("flows:form_settings", clone.pk)
    elif action == "regenerate_link":
        form.token = secrets.token_urlsafe()
        messages.success(request, "New link created — the old one no longer works.")
    elif action == "archive":
        form.archived_at = timezone.now()
        form.save()
        messages.success(request, "Form archived.")
        return redirect("flows:forms")
    elif action == "delete":
        name = form.name
        form.delete()
        messages.success(request, f"Form “{name}” deleted.")
        return redirect("flows:forms")
    form.save()
    return safe_next(request, "flows:form_settings", form.pk)


_NAME_HINTS = ("name", "insured", "policyholder", "customer", "requester", "applicant")
_STATUS_BADGE = {"started": "badge-green", "failed": "badge-red", "unpaid": "badge-amber", "saved": "badge-gray"}
_LIST_LIMIT = 300


def _can_see_submissions(request):
    if not (request.membership.can_create or request.membership.can_view_all):
        raise PermissionDenied("Your role cannot see form submissions.")


def _answers(form, sub):
    """Every answer on a submission, as readable text. Signatures stay as “[signed]”."""
    data = sub.data or {}
    values = data.get("values") or {}
    by_name = data.get("by_name") or {}
    out = []
    for f in form.input_fields():
        label = f.get("label") or f.get("name") or f["id"]
        raw = values.get(f["id"], by_name.get(f.get("name") or label, ""))
        out.append({"label": label, "value": field_value_to_text(f, raw), "type": f.get("type") or ""})
    return out


def _identity(answers):
    """The name (or first short answer) and email shown on the submissions list."""
    name = email = first = ""
    for a in answers:
        if not a["value"] or a["type"] in ("signature", "initials", "attachment"):
            continue
        low = a["label"].lower()
        if not email and (a["type"] == "email" or "email" in low):
            email = a["value"]
        elif not name and any(h in low for h in _NAME_HINTS):
            name = a["value"]
        elif not first:
            first = a["value"]
    title = (name or first or "Submission").strip()
    if email and title == email:
        email = ""
    return title[:120], email


def _haystack(sub, answers, title, email):
    parts = [title, email, sub.get_status_display(), sub.error or ""]
    if sub.packet_id:
        parts.append(sub.packet.name)
    parts.extend(a["value"] for a in answers)
    return " ".join(str(p) for p in parts if p).lower()


def _filtered_submissions(request, form):
    """Submissions for this form after the status, date, and search filters."""
    from django.utils.dateparse import parse_date

    status = request.GET.get("status", "")
    if status not in dict(FormSubmission.STATUSES):
        status = ""
    start = parse_date(request.GET.get("from") or "")
    end = parse_date(request.GET.get("to") or "")
    q = request.GET.get("q", "").strip()
    subs = form.submissions.select_related("packet", "user")
    if status:
        subs = subs.filter(status=status)
    if start:
        subs = subs.filter(created_at__date__gte=start)
    if end:
        subs = subs.filter(created_at__date__lte=end)
    rows = []
    needle = q.lower()
    for sub in subs:
        answers = _answers(form, sub)
        title, email = _identity(answers)
        if needle and needle not in _haystack(sub, answers, title, email):
            continue
        rows.append({"s": sub, "title": title, "email": email, "answers": answers,
                     "badge": _STATUS_BADGE.get(sub.status, "badge-gray")})
    filters = {"q": q, "status": status,
               "from": start.isoformat() if start else "", "to": end.isoformat() if end else ""}
    return rows, filters


@login_required
def form_submissions(request, pk):
    form = _get_form(request, pk)
    _can_see_submissions(request)
    rows, filters = _filtered_submissions(request, form)
    if request.GET.get("format") == "csv":
        labels = []
        for row in rows:
            for a in row["answers"]:
                if a["label"] not in labels and a["type"] not in ("signature", "initials"):
                    labels.append(a["label"])
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Submitted", "Status", "Document"] + labels)
        for row in rows:
            sub = row["s"]
            by_label = {a["label"]: a["value"] for a in row["answers"]}
            w.writerow([timezone.localtime(sub.created_at).strftime("%Y-%m-%d %H:%M"), sub.get_status_display(),
                        sub.packet.name if sub.packet else ""] + [by_label.get(label, "") for label in labels])
        resp = HttpResponse(buf.getvalue(), content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="form-{form.pk}-submissions.csv"'
        return resp
    from urllib.parse import urlencode
    from ..models import Packet

    shown = rows[:_LIST_LIMIT]
    for row in shown:
        row.pop("answers", None)
    visible = set(Packet.objects.visible_to(request.membership).filter(
        pk__in=[r["s"].packet_id for r in shown if r["s"].packet_id]).values_list("pk", flat=True))
    keep = {k: v for k, v in filters.items() if v}
    return render(request, "flows/forms/submissions.html", {
        "form": form, "rows": shown, "matched": len(rows), "truncated": len(rows) > _LIST_LIMIT,
        "filters": filters, "filtered": any(filters.values()), "statuses": FormSubmission.STATUSES,
        "export_qs": urlencode({**keep, "format": "csv"}), "nav": "forms", "visible_packets": visible})


@login_required
def form_submission_panel(request, pk, sub):
    """The rest of a submission: every answer, files, payment, and why it failed."""
    from .. import payments

    form = _get_form(request, pk)
    _can_see_submissions(request)
    sub = get_object_or_404(form.submissions.select_related("packet", "user"), pk=sub)
    answers = _answers(form, sub)
    title, email = _identity(answers)
    pay = sub.payment or {}
    visible = False
    if sub.packet_id:
        from ..models import Packet
        visible = Packet.objects.visible_to(request.membership).filter(pk=sub.packet_id).exists()
    return render(request, "flows/partials/submission_panel.html", {
        "form": form, "sub": sub, "title": title, "email": email, "answers": answers,
        "filled": sum(1 for a in answers if a["value"]), "files": list(sub.files.all()),
        "badge": _STATUS_BADGE.get(sub.status, "badge-gray"), "packet_visible": visible,
        "pay_label": payments.fmt(pay) if pay.get("amount") or pay.get("amount_total") else "",
        "pay_status": (pay.get("status") or "").replace("_", " ")})


# ---- public form -----------------------------------------------------------------------------
def _pay_note(form):
    p = (form.options or {}).get("payment") or {}
    if not p.get("enabled"):
        return ""
    cur = (p.get("currency") or "usd").upper()
    if p.get("amount_field"):
        return f"After you submit, you'll be asked to pay the amount in “{p['amount_field']}” ({cur}) on a secure Stripe page."
    return f"After you submit, you'll be asked to pay {p.get('amount')} {cur} on a secure Stripe page."


def _public_ctx(request, form, values=None, errors=None, preview=False):
    doc = form.as_doc()
    vals = dict(values or {})
    flat = intake.flat_by_name(form, vals)
    view = build_doc_view(doc, vals, editable_ids=intake.editable_ids(form) - {f["id"] for f in form.fields if f.get("readonly")},
                          errors=(errors or {}), all_values=flat)
    for page in view["pages"]:
        for fv in page:
            for x in fv.get("files") or []:
                x["url"] = reverse("flows:form_file", args=[form.token, x["file_id"]])
    ws = form.workspace
    has_sig = any(f.get("type") in ("signature", "initials") for f in form.fields)
    lookups = [{"id": lk["id"], "trigger": lk.get("trigger"), "min_length": lk.get("min_length") or 1,
                "targets": [m.get("to") for m in lk.get("map") or []]} for lk in form.lookups or [] if lk.get("trigger")]
    return {"form": form, "docs": [view], "workspace": ws, "preview": preview, "has_errors": bool(errors),
            "consent_required": has_sig and ws.setting("esign_consent"), "o": form.options or {},
            "lookups_json": _js(lookups), "embed": request.GET.get("embed") == "1",
            "lookup_url": reverse("flows:form_lookup", args=[form.token, "LID"]), "pay_note": _pay_note(form)}


@csrf_exempt  # public form: no signed-in action to protect, and embeds run in cross-site frames
@xframe_options_exempt
def form_public(request, token):
    form = get_object_or_404(IntakeForm, token=token)
    staff_preview = request.GET.get("preview") == "1" and request.user.is_authenticated and \
        getattr(request, "workspace", None) == form.workspace
    o = form.options or {}
    if not staff_preview and (not form.workspace.feature("public_forms") or form.workspace.suspended_at):
        return render(request, "flows/forms/closed.html", {"form": form, "workspace": form.workspace,
                                                           "message": "This form is not available right now."}, status=404)
    if not form.is_live and not staff_preview:
        return render(request, "flows/forms/closed.html", {"form": form, "workspace": form.workspace,
                                                           "message": o.get("closed_message")}, status=404)
    if o.get("require_login") and not request.user.is_authenticated and not staff_preview:
        return redirect(f"{reverse('accounts:login')}?next={request.path}")
    doc = form.as_doc()
    editable = intake.editable_ids(form)
    if request.method == "POST" and not staff_preview:
        session_keys = request.session.get(f"form_files_{form.pk}", [])
        kept = {}
        for f in form.fields:
            if f.get("type") == "attachment":
                kept[f["id"]] = [{"name": ff.original_name, "file_id": ff.key, "form_file": ff.key}
                                 for ff in FormFile.objects.filter(form=form, field_id=f["id"], key__in=session_keys, submission__isnull=True)]
        values, uploads = parse_values(request, None, doc, editable, draft={"form": kept})
        for fid, files in uploads.items():
            from superadmin.models import upload_problem

            max_mb = next((f.get("max_mb") for f in form.fields if f["id"] == fid), 20) or 20
            stored = list(values.get(fid) or [])
            for up in files:
                if upload_problem(up, max_mb):
                    continue
                ff = FormFile.objects.create(form=form, field_id=fid, file=up, original_name=up.name[:255])
                session_keys.append(ff.key)
                stored.append({"name": ff.original_name, "file_id": ff.key, "form_file": ff.key})
            values[fid] = stored
        request.session[f"form_files_{form.pk}"] = session_keys
        intake.rerun_lookups(form, values)
        intake.compute_formulas(form, values)
        flat = intake.flat_by_name(form, values)
        errors = validate(doc, values, editable, flat)
        ctx = _public_ctx(request, form, values, errors)
        if ctx["consent_required"] and request.POST.get("consent") != "on":
            ctx["consent_error"] = "Please agree to use electronic signatures."
            errors["_consent"] = True
            ctx["has_errors"] = True
        if errors:
            return render(request, "flows/forms/public.html", ctx, status=400)
        # visible fields only: drop values of fields hidden by their conditions
        values = {k: v for k, v in values.items()
                  if evaluate(next((f.get("condition") for f in form.fields if f["id"] == k), None), flat)}
        files_by_field = {}
        for fid, items in values.items():
            keys = [x.get("form_file") for x in items or [] if isinstance(x, dict) and x.get("form_file")] if isinstance(items, list) else []
            if keys:
                files_by_field[fid] = list(FormFile.objects.filter(form=form, key__in=keys))
        from .. import payments

        due = payments.amount_for(form, intake.flat_by_name(form, values))
        sub = intake.submit(form, values, files_by_field, user=request.user, ip=engine.client_ip(request), start=due is None)
        request.session[f"form_files_{form.pk}"] = []
        request.session[f"form_sub_{form.pk}"] = sub.pk
        if due:
            amount, currency, label = due
            done = engine.absolute(reverse("flows:form_done", args=[form.token])) + ("?embed=1" if request.GET.get("embed") == "1" else "")
            try:
                email = str(values.get(o.get("recipient_email_field") or "") or "")
                return redirect(payments.checkout(sub, amount, currency, label, done, engine.absolute(form.public_url()), email))
            except payments.PaymentError as exc:
                sub.error = str(exc)[:500]
                sub.save(update_fields=["error"])
                ctx["payment_error"] = f"The payment couldn't be started: {exc} Your answers are saved — please try again later."
                return render(request, "flows/forms/public.html", ctx, status=502)
        if o.get("redirect_url"):
            return redirect(o["redirect_url"])
        return redirect(reverse("flows:form_done", args=[form.token]) + ("?embed=1" if request.GET.get("embed") == "1" else ""))
    return render(request, "flows/forms/public.html", _public_ctx(request, form, preview=staff_preview))


@csrf_exempt
@xframe_options_exempt
@require_POST
def form_lookup(request, token, lookup_id):
    """In-form bot endpoint: {value, values} → {found, values: {field id: value}, message}."""
    form = get_object_or_404(IntakeForm, token=token)
    staff = request.user.is_authenticated and getattr(request, "workspace", None) == form.workspace
    if (not form.is_live or not form.workspace.feature("public_forms") or form.workspace.suspended_at) and not staff:
        raise Http404
    key = f"form-lookup:{form.pk}:{engine.client_ip(request)}"
    hits = cache.get(key, 0)
    if hits >= 40:  # per visitor per minute — lookups expose mapped data, so keep enumeration slow
        return JsonResponse({"found": False, "values": {}, "message": "Too many lookups — wait a minute and try again."}, status=429)
    cache.set(key, hits + 1, 60)
    lk = next((x for x in form.lookups or [] if x.get("id") == lookup_id), None)
    if lk is None:
        raise Http404
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        body = {}
    res = intake.run_lookup(form, lk, body.get("value", ""), body.get("values") or {})
    return JsonResponse(res)


@xframe_options_exempt
def form_done(request, token):
    form = get_object_or_404(IntakeForm, token=token)
    sub = FormSubmission.objects.filter(pk=request.session.get(f"form_sub_{form.pk}") or 0, form=form).first()
    payment = None
    if sub and sub.status == FormSubmission.AWAITING_PAYMENT:
        from .. import payments

        if request.GET.get("session_id") and request.GET["session_id"] == (sub.payment or {}).get("session"):
            try:
                payments.confirm(sub)
            except payments.PaymentError:
                pass
            sub.refresh_from_db()
        payment = "paid" if (sub.payment or {}).get("status") == "paid" else "pending"
    elif sub and (sub.payment or {}).get("status") == "paid":
        payment = "paid"
    return render(request, "flows/forms/done.html", {"form": form, "workspace": form.workspace, "o": form.options or {},
                                                     "sub": sub, "embed": request.GET.get("embed") == "1", "payment": payment})


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """Stripe → checkout.session.completed: settle the submission and start its workflow."""
    from .. import payments

    try:
        event = payments.verify_webhook(request.body, request.headers.get("Stripe-Signature", ""))
    except (payments.PaymentError, ValueError):
        return HttpResponse(status=400)
    if event.get("type") in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        session = (event.get("data") or {}).get("object") or {}
        sub = FormSubmission.objects.filter(pk=str(session.get("client_reference_id") or "0")).first() if str(session.get("client_reference_id") or "").isdigit() else None
        if sub and (sub.payment or {}).get("session") == session.get("id"):
            payments._settle(sub, session)
    return HttpResponse(status=200)


def form_file(request, token, key):
    form = get_object_or_404(IntakeForm, token=token)
    ff = get_object_or_404(FormFile, form=form, key=key)
    allowed = key in request.session.get(f"form_files_{form.pk}", []) or (
        request.user.is_authenticated and getattr(request, "workspace", None) == form.workspace)
    if not allowed:
        raise Http404
    return FileResponse(ff.file.open("rb"), as_attachment=True, filename=ff.original_name)
