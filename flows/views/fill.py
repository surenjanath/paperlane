"""Recipient experience: fill out & sign, save draft, decline, thank-you page, public link, webhook start."""
import json
import re

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .. import engine
from ..conditions import compute_formula, evaluate
from ..fieldtypes import COMPOSITES, is_input
from ..models import Packet, PacketFile, StepRun, Workflow

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


RECIPIENT_LANGS = ("en", "es")


def recipient_language(request, workspace):
    """The recipient's choice (?lang=, remembered), else the workspace default (en / es / auto = browser)."""
    from django.utils import translation

    chosen = request.GET.get("lang")
    if chosen in RECIPIENT_LANGS:
        request.session["recipient_lang"] = chosen
        return chosen
    if request.session.get("recipient_lang") in RECIPIENT_LANGS:
        return request.session["recipient_lang"]
    default = (workspace.settings or {}).get("recipient_language", "en") if workspace else "en"
    if default == "auto":
        lang = translation.get_language_from_request(request)[:2]
        return lang if lang in RECIPIENT_LANGS else "en"
    return default if default in RECIPIENT_LANGS else "en"


def in_recipient_language(view):
    """Render a recipient-facing view in the recipient's language (only these pages are translated)."""
    import functools

    from django.utils import translation

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        ws = None
        token = kwargs.get("token")
        if token:
            run = StepRun.objects.filter(token=token).select_related("packet__workspace").first()
            wf = None if run else Workflow.objects.filter(link_token=token).select_related("workspace").first()
            ws = run.packet.workspace if run else (wf.workspace if wf else None)
        with translation.override(recipient_language(request, ws)):
            response = view(request, *args, **kwargs)
            if hasattr(response, "render") and not getattr(response, "is_rendered", True):
                response.render()
            response.headers.setdefault("Content-Language", translation.get_language())
        return response
    return wrapper


def _input_name(doc_key, fid, *parts):
    return "__".join([f"f_{doc_key}_{fid}", *[str(p) for p in parts]])


def build_doc_view(doc, values, *, editable_ids, errors, all_values=None):
    """Prepare a document for rendering: pages of enriched field dicts."""
    pages, current = [], []
    for f in doc.get("fields", []):
        if f.get("type") == "page_break":
            pages.append(current)
            current = []
            continue
        fv = dict(f)
        fv["value"] = values.get(f["id"], f.get("default", ""))
        fv["editable"] = f["id"] in editable_ids and is_input(f.get("type"))
        fv["error"] = errors.get(f["id"], "")
        fv["input"] = _input_name(doc["key"], f["id"])
        fv["doc_key"] = doc["key"]
        if f.get("type") in COMPOSITES:
            val = fv["value"] if isinstance(fv["value"], dict) else {}
            fv["parts"] = [{"key": k, "label": lbl, "value": val.get(k, ""), "input": _input_name(doc["key"], f["id"], k)}
                           for k, lbl in COMPOSITES[f["type"]]]
        if f.get("type") == "matrix":
            val = fv["value"] if isinstance(fv["value"], dict) else {}
            fv["matrix"] = [{"row": r, "value": val.get(r, ""), "input": _input_name(doc["key"], f["id"], i)}
                            for i, r in enumerate(f.get("rows", []))]
        if f.get("type") == "table":
            rows = fv["value"] if isinstance(fv["value"], list) else []
            n = max(len(rows) + (1 if fv["editable"] else 0), int(f.get("min_rows", 3) or 3))
            fv["table"] = [[{"value": (rows[r] if r < len(rows) else {}).get(c, ""),
                             "input": _input_name(doc["key"], f["id"], f"r{r}", f"c{ci}")}
                            for ci, c in enumerate(f.get("columns", []))] for r in range(n)]
        if f.get("type") in ("group", "repeatable"):
            subs = f.get("subfields", [])
            if f["type"] == "group":
                val = fv["value"] if isinstance(fv["value"], dict) else {}
                fv["group"] = [dict(s, value=val.get(s["id"], ""), input=_input_name(doc["key"], f["id"], s["id"])) for s in subs]
            else:
                rows = fv["value"] if isinstance(fv["value"], list) else []
                n = max(len(rows) + (1 if fv["editable"] else 0), 1)
                fv["repeat"] = [[dict(s, value=(rows[r] if r < len(rows) else {}).get(s["id"], ""),
                                      input=_input_name(doc["key"], f["id"], r, s["id"])) for s in subs] for r in range(n)]
        if f.get("type") == "image_choice":
            imgs = f.get("images") or []
            fv["choices"] = [{"option": o, "image": imgs[i] if i < len(imgs) else ""} for i, o in enumerate(f.get("options", []))]
        if f.get("type") == "rating":
            try:
                top = max(3, min(10, int(f.get("max") or 5)))
            except (TypeError, ValueError):
                top = 5
            fv["stars"] = list(range(top, 0, -1))
        if f.get("type") == "attachment":
            v = fv["value"]
            fv["files"] = [x for x in (v if isinstance(v, list) else [v]) if isinstance(x, dict) and x.get("file_id")]
        if f.get("type") == "date" and f.get("date_format"):
            from ..fieldtypes import format_date

            fv["display"] = format_date(fv["value"], f["date_format"]) if fv["value"] else ""
        if f.get("type") == "checkbox" and f.get("name") and f.get("options"):
            keys = f.get("option_values") or f["options"]
            fv["option_keys"] = {label: f"{f['name']}-{k}" for label, k in zip(f["options"], keys)}
        if f.get("type") == "checkbox":
            v = fv["value"]
            fv["checked"] = v if isinstance(v, list) else ([v] if v else [])
        if f.get("type") == "youtube" and f.get("url"):
            m = re.search(r"(?:v=|youtu\.be/|embed/)([\w-]{6,})", f["url"])
            fv["embed"] = f"https://www.youtube.com/embed/{m.group(1)}" if m else ""
        if f.get("type") == "vimeo" and f.get("url"):
            m = re.search(r"vimeo\.com/(?:video/)?(\d+)", f["url"])
            fv["embed"] = f"https://player.vimeo.com/video/{m.group(1)}" if m else ""
        if f.get("type") == "formula" and all_values is not None:
            fv["value"] = compute_formula(f.get("formula", ""), all_values) or fv["value"]
        current.append(fv)
    pages.append(current)
    return {"key": doc["key"], "name": doc["name"], "design": doc.get("design", {}), "file_url": doc.get("file_url", ""),
            "pages": [p for p in pages if p] or [[]]}


def parse_values(request, packet, doc, editable_ids, draft=None):
    """Read posted values for editable fields of ``doc``. Returns (values, files).

    ``draft`` holds this step's unsubmitted values; attachments stored there (e.g. uploaded on a submit that
    failed validation) count as existing files so they are not dropped on the next submit."""
    p, out, files = request.POST, {}, {}
    for f in doc.get("fields", []):
        fid, t = f["id"], f.get("type")
        if fid not in editable_ids or not is_input(t):
            continue
        name = _input_name(doc["key"], fid)
        if t == "checkbox":
            opts = f.get("options") or []
            vals = p.getlist(name)
            out[fid] = vals if opts else bool(vals)
        elif t == "clickwrap":
            out[fid] = p.get(name) == "on"
        elif t in COMPOSITES:
            out[fid] = {k: p.get(_input_name(doc["key"], fid, k), "").strip() for k, _ in COMPOSITES[t]}
        elif t == "matrix":
            out[fid] = {r: p.get(_input_name(doc["key"], fid, i), "") for i, r in enumerate(f.get("rows", []))}
        elif t == "table":
            rows, r = [], 0
            cols = f.get("columns", [])
            while _input_name(doc["key"], fid, f"r{r}", "c0") in p and r < 500:
                row = {c: p.get(_input_name(doc["key"], fid, f"r{r}", f"c{ci}"), "").strip() for ci, c in enumerate(cols)}
                if any(row.values()):
                    rows.append(row)
                r += 1
            out[fid] = rows
        elif t == "group":
            out[fid] = {s["id"]: p.get(_input_name(doc["key"], fid, s["id"]), "").strip() for s in f.get("subfields", [])}
        elif t == "repeatable":
            rows, r = [], 0
            subs = f.get("subfields", [])
            while subs and _input_name(doc["key"], fid, r, subs[0]["id"]) in p and r < 200:
                row = {s["id"]: p.get(_input_name(doc["key"], fid, r, s["id"]), "").strip() for s in subs}
                if any(row.values()):
                    rows.append(row)
                r += 1
            out[fid] = rows
        elif t == "attachment":
            existing = []
            for src in ((packet.data if packet else {}) or {}, draft or {}):
                v = (src.get(doc["key"]) or {}).get(fid)
                for x in (v if isinstance(v, list) else [v]):
                    if isinstance(x, dict) and x.get("file_id") and all(e.get("file_id") != x["file_id"] for e in existing):
                        existing.append(x)
            keep = set(p.getlist(name + "__keep")) if (name + "__keep_present") in p else {str(x.get("file_id")) for x in existing}
            out[fid] = [x for x in existing if str(x.get("file_id")) in keep]
            uploads = request.FILES.getlist(name)
            if uploads:
                files[fid] = uploads
        elif t == "formula":
            continue
        else:
            out[fid] = p.get(name, "").strip()
    return out, files


def validate(doc, values, editable_ids, flat):
    errors = {}
    for f in doc.get("fields", []):
        fid, t = f["id"], f.get("type")
        if fid not in editable_ids or not is_input(t) or t == "formula":
            continue
        if not evaluate(f.get("condition"), flat):
            continue
        v = values.get(fid)
        empty = v in (None, "", [], {}, False) or (isinstance(v, dict) and not any(v.values()))
        if f.get("required") and empty:
            errors[fid] = "This field is required."
            continue
        if t == "attachment" and isinstance(v, list):
            if f.get("max_files") and len(v) > int(f["max_files"]):
                errors[fid] = f"You can upload up to {f['max_files']} attachments."
        if empty:
            continue
        if t == "email" and not EMAIL_RE.match(str(v)):
            errors[fid] = "Enter a valid email address."
        if t in ("number", "currency"):
            try:
                n = float(str(v).replace(",", "").replace("$", ""))
                if f.get("min") not in (None, "") and n < float(f["min"]):
                    errors[fid] = f"Must be at least {f['min']}."
                if f.get("max") not in (None, "") and n > float(f["max"]):
                    errors[fid] = f"Must be at most {f['max']}."
            except ValueError:
                errors[fid] = "Enter a number."
        if t in ("text", "textarea") and f.get("max_length") and len(str(v)) > int(f["max_length"]):
            errors[fid] = f"Maximum {f['max_length']} characters."
        if t in ("text",) and f.get("pattern"):
            try:
                if not re.fullmatch(f["pattern"], str(v)):
                    errors[fid] = f.get("pattern_message") or "Invalid format."
            except re.error:
                pass
    return errors


def _context(run, values_override=None, errors=None):
    packet = run.packet
    step = packet.step(run.step_key) or {}
    editable = engine.editable_field_ids(packet, run.step_key)
    data = {k: dict(v) for k, v in (packet.data or {}).items()}
    for dk, vals in (values_override or run.draft or {}).items():
        data.setdefault(dk, {}).update(vals)
    flat = dict(packet.flat_values())
    for doc in packet.documents:
        for f in doc.get("fields", []):
            if f["id"] in data.get(doc["key"], {}):
                v = data[doc["key"]][f["id"]]
                flat[f["id"]] = v
                if f.get("name"):
                    flat[f["name"]] = v
                if f.get("label"):
                    flat[f["label"]] = v
    docs = [build_doc_view(d, data.get(d["key"], {}), editable_ids=editable.get(d["key"], set()),
                           errors=(errors or {}).get(d["key"], {}), all_values=flat)
            for d in engine.visible_documents(packet, run.step_key)]
    ws = packet.workspace
    for d in docs:  # what this person has to do in each document (drives the tabs and where the page opens)
        fields = [fv for pg in d["pages"] for fv in pg]
        d["n_edit"] = sum(1 for fv in fields if fv["editable"])
        d["n_req"] = sum(1 for fv in fields if fv["editable"] and fv.get("required"))
        d["n_left"] = sum(1 for fv in fields if fv["editable"] and fv.get("required") and fv.get("value") in ("", None, [], {}))
    # open on the first document that still needs required input, else the first one this person can edit
    start_doc = next((i for i, d in enumerate(docs) if d["n_left"]), next((i for i, d in enumerate(docs) if d["n_edit"]), 0))
    done, total = packet.progress()
    prev = packet.runs.filter(status=StepRun.COMPLETED).exclude(pk=run.pk).order_by("-completed_at").first()
    return {
        "start_doc": start_doc, "n_req": sum(d["n_req"] for d in docs), "n_left": sum(d["n_left"] for d in docs), "n_fill_docs": sum(1 for d in docs if d["n_edit"]),
        "step_no": min(done + 1, total), "step_total": total,
        # staff see who handed it over; outside recipients see the organisation, never internal names
        "handed_by": (prev.assignee_name or prev.assignee_email) if prev else engine.actor_label(packet.created_by, ws.name),
        "run": run, "packet": packet, "step": step, "docs": docs, "workspace": ws, "wf": packet.workflow,
        "settings": step.get("settings") or {},
        "consent_required": packet.definition.get("esign_consent") or ws.setting("esign_consent"),
        "autofill_signature": ws.setting("autofill_signatures"),
        "profile": getattr(run.assignee_user, "profile", None) if run.assignee_user else None,
        "has_errors": bool(errors),
        "ai_assist": _ai_assist_on(ws),
    }


def _ai_assist_on(ws):
    from .. import ai_actions

    return ai_actions.enabled(ws, "fill_assist") and bool(ai_actions.settings_for(ws).get("engine") or ai_actions.ai_providers.default_engine() != "planner")


@in_recipient_language
def fill(request, token):
    run = get_object_or_404(StepRun.objects.select_related("packet", "packet__workflow", "packet__workspace"), token=token)
    packet = run.packet
    if packet.status != Packet.IN_PROGRESS or not run.is_open:
        return render(request, "flows/fill_closed.html", {"run": run, "packet": packet, "workspace": packet.workspace})
    if run.expires_at and run.expires_at < timezone.now():
        run.status = StepRun.EXPIRED
        run.save(update_fields=["status"])
        return render(request, "flows/fill_closed.html", {"run": run, "packet": packet, "workspace": packet.workspace})
    user = request.user if request.user.is_authenticated else None
    wf = packet.workflow
    if run.claimed_by_id and (user is None or user.id != run.claimed_by_id):
        membership = getattr(request, "membership", None)
        manager = bool(membership and membership.workspace_id == packet.workspace_id and membership.can_manage)
        if not (manager and request.method == "POST" and request.POST.get("action") == "takeover"):
            return render(request, "flows/fill_taken.html", {"run": run, "packet": packet, "workspace": packet.workspace,
                                                             "manager": manager}, status=409)
        previous = run.claimed_by
        run.claim(user)
        engine.notify(previous, packet.workspace, f"{engine.actor_label(user)} took over {packet.name}", run.step_name,
                      f"/documents/{packet.pk}/", kind="assigned")
        engine.audit(packet.workspace, "step.taken_over", f"{run.step_name}: {previous.email} → {user.email}", actor=user,
                     packet=packet, request=request)
        return redirect("flows:fill", run.token)
    if wf.welcome_enabled and wf.welcome_message and not request.session.get(f"welcomed_{run.pk}") and request.method == "GET":
        if request.GET.get("start") != "1":
            return render(request, "flows/fill_welcome.html", {"run": run, "packet": packet, "wf": wf, "workspace": packet.workspace})
        request.session[f"welcomed_{run.pk}"] = True
    from .. import identity

    v = identity.rule(run)
    if v and not identity.passed(request, run):
        return _verify(request, run, v)
    engine.mark_opened(run, user=user, request=request)
    packet.refresh_from_db()
    run.refresh_from_db()
    if request.method == "POST":
        return _handle_post(request, run, user)
    return render(request, "flows/fill.html", _context(run))


def _verify(request, run, v):
    """The identity check page in front of the documents."""
    from .. import identity

    ctx = {"run": run, "packet": run.packet, "workspace": run.packet.workspace, "method": v["method"],
           "dest": identity.masked(identity.destination(run, v)), "locked": identity.locked(run)}
    if request.method == "POST":
        if request.POST.get("idv") == "send" and v["method"] != "code":
            ok, ctx["note"] = identity.send_code(run, v)
            ctx["sent"] = ok
        elif request.POST.get("idv") == "check":
            if identity.check(request, run, v, request.POST.get("code")):
                return redirect("flows:fill", run.token)
            ctx["error"] = ("Too many wrong attempts. Try again in 15 minutes." if identity.locked(run)
                            else "That code isn't right. Check it and try again.")
            ctx["sent"] = True
    return render(request, "flows/fill_verify.html", ctx, status=200)


def _claim_action(request, run, user, action):
    packet = run.packet
    if user is None or not run.assignee_group_id:
        return redirect("flows:fill", run.token)
    if action == "claim" and run.can_act(user):
        run.claim(user)
        engine.audit(packet.workspace, "step.claimed", f"{run.step_name} taken by {user.email}", actor=user, packet=packet,
                     request=request)
        messages.success(request, "You took this task — your teammates won't see it in their inbox while you work on it.")
    elif action == "release" and run.claimed_by_id == user.id:
        run.release()
        engine.audit(packet.workspace, "step.released", f"{run.step_name} released by {user.email}", actor=user, packet=packet,
                     request=request)
        messages.info(request, f"Released — it's back in the {run.assignee_group.name} queue.")
        return redirect("core:inbox")
    return redirect("flows:fill", run.token)


def _handle_post(request, run, user):
    packet = run.packet
    step = packet.step(run.step_key) or {}
    s = step.get("settings") or {}
    action = request.POST.get("action", "submit")
    if action in ("claim", "release"):
        return _claim_action(request, run, user, action)
    if user is not None and run.assignee_group_id and not run.claimed_by_id and action in ("submit", "draft"):
        run.claim(user)  # working on a group step takes it
    if action == "decline" and not s.get("forbid_decline"):
        engine.decline_run(run, request.POST.get("reason", "").strip(), user=user, request=request)
        return redirect("flows:fill_done", run.token)
    if action == "discard" and not s.get("forbid_discard"):
        run.draft = {}
        run.save(update_fields=["draft"])
        messages.info(request, "Your changes were discarded.")
        return redirect("flows:fill", run.token)
    editable = engine.editable_field_ids(packet, run.step_key)
    values_by_doc, files_by_doc, errors = {}, {}, {}
    visible = engine.visible_documents(packet, run.step_key)
    for doc in visible:
        vals, files = parse_values(request, packet, doc, editable.get(doc["key"], set()), draft=run.draft)
        values_by_doc[doc["key"]] = vals
        files_by_doc[doc["key"]] = files
    size_errors = {}
    for doc in visible:  # store uploads first so "required" passes
        fields = {f["id"]: f for f in doc.get("fields", [])}
        for fid, uploads in files_by_doc[doc["key"]].items():
            from superadmin.models import upload_problem

            stored = list(values_by_doc[doc["key"]].get(fid) or [])
            for upload in uploads:
                problem = upload_problem(upload, fields.get(fid, {}).get("max_mb"))
                if problem:
                    size_errors.setdefault(doc["key"], {})[fid] = problem
                    continue
                pf = PacketFile.objects.create(packet=packet, doc_key=doc["key"], field_id=fid, file=upload, original_name=upload.name[:255])
                stored.append({"name": pf.original_name, "file_id": pf.pk})
            values_by_doc[doc["key"]][fid] = stored
    if action == "draft" and not s.get("forbid_draft"):
        run.draft = values_by_doc
        run.save(update_fields=["draft"])
        engine.audit(packet.workspace, "step.draft", f"Draft saved for {run.step_name}", actor=user, packet=packet, request=request,
                     label=engine.actor_label(user, run.assignee_email))
        messages.success(request, "Draft saved. You can come back to this link at any time.")
        return redirect("flows:fill", run.token)
    # compute flat values including the new input for conditions/formulas
    merged = {k: dict(v) for k, v in packet.data.items()}
    for dk, vals in values_by_doc.items():
        merged.setdefault(dk, {}).update(vals)
    tmp = Packet(definition=packet.definition, data=merged)
    flat = tmp.flat_values()
    for doc in visible:
        for f in doc.get("fields", []):
            if f.get("type") == "formula" and f["id"] in editable.get(doc["key"], set()):
                values_by_doc[doc["key"]][f["id"]] = compute_formula(f.get("formula", ""), flat)
        errs = {**validate(doc, values_by_doc[doc["key"]], editable.get(doc["key"], set()), flat), **size_errors.get(doc["key"], {})}
        if errs:
            errors[doc["key"]] = errs
    ctx = _context(run, values_by_doc, errors)
    if ctx["consent_required"] and request.POST.get("consent") != "on":
        ctx["consent_error"] = "Please agree to use electronic signatures."
        errors["_consent"] = True
    if errors:
        # Keep this submit's attachments with the step so the recipient does not have to upload them again.
        draft = {k: dict(v) for k, v in (run.draft or {}).items()}
        for doc in visible:
            for f in doc.get("fields", []):
                if f.get("type") == "attachment" and f["id"] in values_by_doc[doc["key"]]:
                    draft.setdefault(doc["key"], {})[f["id"]] = values_by_doc[doc["key"]][f["id"]]
        if draft != (run.draft or {}):
            run.draft = draft
            run.save(update_fields=["draft"])
        ctx["has_errors"] = True
        return render(request, "flows/fill.html", ctx, status=400)
    engine.complete_run(run, values_by_doc, user=user, request=request)
    if s.get("redirect_url"):
        return redirect(s["redirect_url"])
    return redirect("flows:fill_done", run.token)


@in_recipient_language
def fill_done(request, token):
    run = get_object_or_404(StepRun.objects.select_related("packet", "packet__workspace"), token=token)
    packet = run.packet
    step = packet.step(run.step_key) or {}
    ws = packet.workspace
    from accounts.models import default_thank_you

    ty = {**default_thank_you(), **(ws.thank_you or {})}
    for k, v in (step.get("thank_you") or {}).items():
        if v:
            ty[k] = v
    from .. import mailer

    tctx = {"document_name": packet.name, "reference": f"#{packet.pk}", "workspace_name": ws.name,
            "workflow_name": packet.workflow.name, "step_name": run.step_name, "fields": packet.flat_values()}
    for k in ("title", "body", "next_steps", "contact"):
        ty[k] = mailer.render(ty.get(k) or "", tctx)
    ty["steps_list"] = [ln.strip(" -•*\t") for ln in (ty.get("next_steps") or "").splitlines() if ln.strip(" -•*\t")]
    next_run = None
    if ws.setting("multiple_documents") and request.user.is_authenticated:
        from core.queries import my_runs

        next_run = (my_runs(request.user, ws).filter(status__in=["pending", "opened"], packet__status="in_progress")
                    .exclude(pk=run.pk).order_by("created_at").first())
    can_register = (ws.setting("recipient_registration") and not request.user.is_authenticated and run.assignee_email
                    and not get_user_model().objects.filter(email__iexact=run.assignee_email).exists())
    own_next = packet.runs.filter(status__in=["pending", "opened"]).first()
    if own_next and not own_next.can_act(request.user) and not packet.is_test:
        own_next = None
    return render(request, "flows/fill_done.html", {"run": run, "packet": packet, "workspace": ws, "ty": ty,
                                                    "next_run": next_run, "own_next": own_next, "can_register": can_register})


def download_file(request, file_id):
    pf = get_object_or_404(PacketFile.objects.select_related("packet"), pk=file_id)
    allowed = False
    token = request.GET.get("t")
    if token and pf.packet.runs.filter(token=token).exists():
        allowed = True
    elif request.user.is_authenticated and getattr(request, "membership", None):
        allowed = Packet.objects.visible_to(request.membership).filter(pk=pf.packet_id).exists()
    if not allowed:
        raise Http404
    return FileResponse(pf.file.open("rb"), as_attachment=True, filename=pf.original_name)


@in_recipient_language
def public_start(request, token):
    wf = get_object_or_404(Workflow, link_token=token, trashed_at__isnull=True, archived_at__isnull=True)
    if not wf.link_enabled or not wf.is_published or not wf.workspace.feature("public_forms") or wf.workspace.suspended_at:
        return render(request, "flows/fill_closed.html", {"workspace": wf.workspace, "link_disabled": True, "wf": wf}, status=404)
    if request.method == "POST":
        email = request.POST.get("email", "").strip()
        name = request.POST.get("name", "").strip()
        user = request.user if request.user.is_authenticated else None
        if user and not email:
            email, name = user.email, engine.actor_label(user)
        assignee = {"email": email, "name": name or email or "Link recipient",
                    "user": get_user_model().objects.filter(email__iexact=email).first() if email else None}
        packet = engine.start_packet(wf, user=wf.created_by, source="link", first_assignee=assignee, request=request,
                                     name=f"{wf.name} – {name or email or 'link'}")
        run = packet.runs.filter(status="pending").first()
        if run:
            return redirect("flows:fill", run.token)
        return render(request, "flows/fill_done.html", {"packet": packet, "workspace": wf.workspace, "ty": wf.workspace.thank_you})
    return render(request, "flows/public_start.html", {"wf": wf, "workspace": wf.workspace})


@csrf_exempt
@require_POST
def hook(request, token):
    """External event autostart: POST JSON {field: value, ...} (optionally {"recipient": email, "name": ...})."""
    wf = Workflow.objects.filter(hook_token=token, trashed_at__isnull=True, archived_at__isnull=True).first()
    if wf is None or not wf.is_published:
        return JsonResponse({"ok": False, "error": "Unknown or unpublished workflow"}, status=404)
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        payload = request.POST.dict()
    if not isinstance(payload, dict):
        return JsonResponse({"ok": False, "error": "Body must be a JSON object"}, status=400)
    recipient = payload.pop("recipient", "")
    assignee = {"email": recipient, "name": payload.pop("recipient_name", recipient),
                "user": get_user_model().objects.filter(email__iexact=recipient).first()} if recipient else None
    packet = engine.start_packet(wf, user=wf.created_by, source="webhook", prefill=payload, first_assignee=assignee,
                                 name=payload.pop("document_name", None), request=request)
    if assignee:
        from .library import _send_invites

        _send_invites(packet)
    if wf.created_by:
        engine.notify(wf.created_by, wf.workspace, f"Started automatically: {wf.name}",
                      "Started from an external event.", reverse("flows:packet_detail", args=[packet.pk]), kind="autostart")
    return JsonResponse({"ok": True, "document_id": packet.pk, "status": packet.status,
                         "fill_links": [r.absolute_fill_url() for r in packet.runs.filter(status="pending")]})


@require_POST
def save_signature(request):
    """Store the signed-in user's default signature / initials (used by the profile page and auto-fill)."""
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False}, status=403)
    kind = request.POST.get("kind")
    data = request.POST.get("data", "")
    if kind not in ("signature", "initials") or not data.startswith("data:image/"):
        return JsonResponse({"ok": False}, status=400)
    setattr(request.user.profile, kind, data)
    request.user.profile.save(update_fields=[kind])
    return JsonResponse({"ok": True})


ASSIST_TYPES = {"text", "textarea", "number", "currency", "date", "email", "phone", "url", "dropdown", "select", "radio",
                "checkbox", "time"}


@require_POST
def ai_extract(request, token):
    """✦ Fill from text: read text the recipient pasted into the fields they may edit on this step (nothing is saved)."""
    from .. import ai_actions
    from ..models import AIUsage

    run = get_object_or_404(StepRun.objects.select_related("packet", "packet__workspace"), token=token)
    packet = run.packet
    if packet.status != Packet.IN_PROGRESS or not run.is_open:
        return JsonResponse({"error": "This step is closed."}, status=409)
    if run.claimed_by_id and (not request.user.is_authenticated or request.user.id != run.claimed_by_id):
        return JsonResponse({"error": "Someone else is working on this step."}, status=409)
    if not ai_actions.enabled(packet.workspace, "fill_assist"):
        return JsonResponse({"error": "Fill from text is turned off."}, status=403)
    if AIUsage.objects.filter(packet=packet, kind="fill_assist", created_at__gte=timezone.now() - timezone.timedelta(hours=1)).count() >= 10:
        return JsonResponse({"error": "Too many requests — try again later."}, status=429)
    text = request.POST.get("text", "").strip()
    if len(text) < 10:
        return JsonResponse({"error": "Paste some text first."}, status=400)
    editable = engine.editable_field_ids(packet, run.step_key)
    fields = {d["key"]: [f for f in d.get("fields", []) if f["id"] in editable.get(d["key"], set())
                         and f.get("type") in ASSIST_TYPES and f.get("type") not in COMPOSITES]
              for d in engine.visible_documents(packet, run.step_key)}
    try:
        values = ai_actions.extract_from_text(packet, {k: v for k, v in fields.items() if v}, text,
                                              user=request.user if request.user.is_authenticated else None)
    except ai_actions.AIError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    inputs = {_input_name(dk, fid): v for dk, vals in values.items() for fid, v in vals.items()}
    return JsonResponse({"values": inputs, "count": len(inputs)})
