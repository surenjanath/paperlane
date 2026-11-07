"""Document editor (form creator) — elements, widgets, pages, properties, design, conditions, preview."""
import json

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.urls import reverse
from django.shortcuts import get_object_or_404, redirect, render
from django.db import models
from django.views.decorators.http import require_POST

from ..fieldtypes import COMPOSITES, CONDITION_OPERATORS, ELEMENTS, WIDGETS
from ..models import Step, Workflow, WorkflowDocument
from ..permissions import get_workflow
from .builder import _editable


def _js(value):
    return json.dumps(value).replace("</", "<\\/")


@login_required
def editor(request, pk, doc_id):
    wf = get_workflow(request, pk, edit=True)
    holder = wf.lock_holder(request.user)
    if holder is not None:  # same choice as the builder: wait, ask, or take over
        if request.GET.get("takeover") != "1":
            return render(request, "flows/in_use.html", {"wf": wf, "holder": holder})
        from .. import engine

        engine.notify(holder, wf.workspace, f"{engine.actor_label(request.user)} took over configuring {wf.name}", "",
                      reverse("flows:workflow_detail", args=[wf.pk]), kind="access_request")
        wf.locked_by = None
        wf.save(update_fields=["locked_by"])
    wf = _editable(request, pk)
    doc = get_object_or_404(WorkflowDocument, workflow=wf, pk=doc_id)
    roles = [{"key": s.key, "name": s.name} for s in wf.ordered_steps() if s.kind == Step.RECIPIENT]
    profile_design = (request.workspace.theme or {}).get("default_form_design") or {}
    back = reverse("flows:builder", args=[wf.pk]) if wf.kind == "workflow" else wf.get_absolute_url()
    ed = {"kind": "Document", "name": doc.name, "back_url": back,
          "save_url": reverse("flows:editor_save", args=[wf.pk, doc.pk]),
          "ai_url": reverse("flows:editor_ai", args=[wf.pk, doc.pk]),
          "continue_url": reverse("flows:editor_continue", args=[wf.pk, doc.pk]),
          "preview_url": reverse("flows:preview", args=[wf.pk, doc.pk]),
          "continue_label": "Continue to send" if wf.kind == "document" else "Continue",
          "side_title": "Other documents in this workflow",
          "side_items": [(d.name, reverse("flows:editor", args=[wf.pk, d.pk]), d.pk == doc.pk) for d in wf.documents.all()]}
    return render(request, "flows/editor.html", {
        "ed": ed, "wf": wf, "doc": doc, "elements": ELEMENTS, "widgets": WIDGETS,
        "schema_json": _js(doc.schema or {"fields": []}),
        "design_json": _js(doc.design or profile_design),
        "roles_json": _js(roles), "composites_json": _js(COMPOSITES),
        "ops_json": _js(CONDITION_OPERATORS), "all_docs": wf.documents.all(),
    })


@login_required
@require_POST
def editor_save(request, pk, doc_id):
    wf = _editable(request, pk)
    doc = get_object_or_404(WorkflowDocument, workflow=wf, pk=doc_id)
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)
    schema = payload.get("schema") or {}
    fields = schema.get("fields")
    if not isinstance(fields, list):
        return JsonResponse({"ok": False, "error": "schema.fields must be a list"}, status=400)
    seen = set()
    for f in fields:
        if not isinstance(f, dict) or not f.get("id") or not f.get("type") or f["id"] in seen:
            return JsonResponse({"ok": False, "error": "Every field needs a unique id and a type"}, status=400)
        seen.add(f["id"])
    doc.schema = {"fields": fields, "roles": schema.get("roles", [])}
    doc.design = payload.get("design") or {}
    if payload.get("name"):
        doc.name = str(payload["name"])[:200]
    doc.save()
    if payload.get("set_default_design"):
        theme = dict(request.workspace.theme or {})
        theme["default_form_design"] = doc.design
        request.workspace.theme = theme
        request.workspace.save(update_fields=["theme"])
    # new roles added from the editor become recipient steps
    for role in payload.get("new_roles", []):
        Step.objects.create(workflow=wf, name=str(role.get("name") or "Recipient")[:120], key=role["key"],
                            order=(wf.steps.order_by("-order").values_list("order", flat=True).first() or 0) + 10)
    wf.touch()
    return JsonResponse({"ok": True})


@login_required
def editor_continue(request, pk, doc_id):
    wf = get_workflow(request, pk, edit=True)
    if wf.kind == Workflow.DOCUMENT:
        return redirect("flows:document_send", wf.pk)
    if wf.kind == Workflow.TEMPLATE:
        return redirect(wf)
    return redirect("flows:builder", wf.pk)


@login_required
def preview(request, pk, doc_id):
    wf = get_workflow(request, pk, edit=True)
    doc = get_object_or_404(WorkflowDocument, workflow=wf, pk=doc_id)
    from .fill import build_doc_view

    view_doc = build_doc_view(doc.as_dict(), {}, editable_ids={f["id"] for f in doc.fields}, errors={})
    return render(request, "flows/fill.html", {"preview": True, "docs": [view_doc], "wf": wf,
                                               "workspace": wf.workspace, "packet": None, "run": None, "step": {}})


@login_required
@require_POST
def editor_ai(request, pk, doc_id):
    """AI document builder. action = build | edit | review | apply (multipart; fields/roles as JSON).
    Nothing is saved here — the editor applies the returned fields to its own state (with undo) and saves as usual."""

    wf = _editable(request, pk)
    get_object_or_404(WorkflowDocument, workflow=wf, pk=doc_id)
    return run_editor_ai(request, wf.workspace)


def run_editor_ai(request, ws):
    """Shared by the document editor and the start-form editor."""
    from .. import ai_actions, ai_forms

    if not ai_actions.enabled(ws):
        return JsonResponse({"error": "AI is turned off for this workspace (My workspace › AI & models)."}, status=403)
    p = request.POST
    try:
        fields = json.loads(p.get("fields") or "[]")
        roles = [r for r in json.loads(p.get("roles") or "[]") if isinstance(r, dict) and r.get("key") and r.get("name")]
    except json.JSONDecodeError:
        return JsonResponse({"error": "Bad request"}, status=400)
    if not isinstance(fields, list):
        return JsonResponse({"error": "Bad request"}, status=400)
    default_role = p.get("role") or "anyone"
    action = p.get("action")
    try:
        if action == "build":
            upload = request.FILES.get("file")
            source, images = p.get("source", "")[:20000], None
            if upload:
                text = ai_forms.text_from_upload(upload)
                if text is None:
                    if not ai_forms.vision_engine(ws):
                        return JsonResponse({"error": "Reading images needs a vision model (e.g. `ollama pull qwen2.5vl:3b`). "
                                                      "Upload a PDF or Word file, or paste the form's text instead."}, status=400)
                    images = [ai_forms.image_b64(upload)]
                else:
                    source = text
            if not (p.get("text", "").strip() or source or images):
                return JsonResponse({"error": "Describe the document, or upload / paste an existing form."}, status=400)
            res = ai_forms.generate(ws, p.get("text", "")[:3000], roles, default_role, request.user, source, images)
        elif action == "edit":
            if not p.get("text", "").strip():
                return JsonResponse({"error": "Describe the change."}, status=400)
            res = ai_forms.edit(ws, fields, p["text"], roles, default_role, request.user)
        elif action == "review":
            res = {"suggestions": ai_forms.review(ws, fields, roles, request.user)}
        elif action == "apply":  # a review suggestion's edits — no AI call
            res = ai_forms.apply_edits(fields, json.loads(p.get("edits") or "[]"), roles, default_role)
        else:
            return JsonResponse({"error": "Unknown action"}, status=400)
    except (ai_forms.FormAIError, ai_actions.AIError, ValueError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(res)


# ---- field library ----------------------------------------------------------------------------------------------
@login_required
def field_library(request):
    """GET → starter + workspace sections; POST (JSON) → save a section; ?delete=<id> (POST) → remove one."""
    from ..field_library import STARTERS
    from ..models import FieldBlock

    ws = request.workspace
    if request.method == "POST":
        if not request.membership.can_create:
            return JsonResponse({"error": "Your role can't change the library."}, status=403)
        if request.GET.get("delete"):
            FieldBlock.objects.filter(workspace=ws, pk=request.GET["delete"]).delete()
            return JsonResponse({"ok": True})
        if request.GET.get("used"):
            FieldBlock.objects.filter(workspace=ws, pk=request.GET["used"]).update(uses=models.F("uses") + 1)
            return JsonResponse({"ok": True})
        try:
            body = json.loads(request.body or "{}")
        except json.JSONDecodeError:
            return JsonResponse({"error": "Bad request"}, status=400)
        fields = [f for f in body.get("fields") or [] if isinstance(f, dict) and f.get("id") and f.get("type")][:60]
        name = str(body.get("name") or "").strip()[:120]
        if not fields or not name:
            return JsonResponse({"error": "Name the section and include at least one field."}, status=400)
        for f in fields:
            f.pop("role", None)  # the form it's dropped into decides who fills it
        b = FieldBlock.objects.create(workspace=ws, name=name, description=str(body.get("description") or "")[:300], fields=fields,
                                      created_by=request.user)
        return JsonResponse({"ok": True, "id": b.pk})
    mine = [{"id": b.pk, "name": b.name, "description": b.description, "fields": b.fields, "count": len(b.fields), "custom": True}
            for b in FieldBlock.objects.filter(workspace=ws)[:100]]
    starters = [{**x, "count": len(x["fields"]), "custom": False} for x in STARTERS]
    return JsonResponse({"blocks": mine + starters})
