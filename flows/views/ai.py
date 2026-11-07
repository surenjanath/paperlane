"""Workflows › Describe it: build a workflow from a plain-language description (Claude or the built-in planner)."""
import json
import time

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from .. import ai_builder, ai_providers, engine
from ..blueprint import BlueprintError, build_workflow
from ..permissions import require

MAX_TEXT = 4000


def _ctx(request, **extra):
    return {"nav": "workflows", "examples": ai_builder.EXAMPLES, **extra}


@login_required
@require("create")
def describe(request):
    if request.method != "POST":
        return render(request, "flows/ai_describe.html", _ctx(request, text=request.GET.get("text", "")[:MAX_TEXT]))
    action = request.POST.get("action", "plan")
    text = request.POST.get("text", "").strip()[:MAX_TEXT]
    if action == "plan":
        from .. import ai_actions

        try:
            chosen = ai_actions.engine_for(request.workspace)
        except ai_actions.AIError:
            chosen = ai_providers.PLANNER
        try:
            started = time.monotonic()
            with ai_providers.bind(ai_actions.settings_for(request.workspace)):
                result = ai_builder.plan(text, request.workspace, request.user, engine=chosen)
            result["seconds"] = round(time.monotonic() - started, 1)
            preview = ai_builder.dry_run(request.workspace, request.user, result["blueprint"])
        except (BlueprintError, ValueError) as exc:
            return render(request, "flows/ai_describe.html", _ctx(request, text=text, error=str(exc)))
        return render(request, "flows/ai_describe.html", _ctx(
            request, text=text, result=result, preview=preview,
            blueprint_json=json.dumps(result["blueprint"], indent=1, ensure_ascii=False)))
    # create (optionally from an edited blueprint)
    try:
        blueprint = json.loads(request.POST.get("blueprint", ""))
    except json.JSONDecodeError as exc:
        return render(request, "flows/ai_describe.html", _ctx(request, text=text, error=f"The blueprint JSON is invalid: {exc.msg} (line {exc.lineno})."))
    try:
        wf, problems = build_workflow(request.workspace, request.user, blueprint, publish=request.POST.get("publish") == "1")
    except BlueprintError as exc:
        return render(request, "flows/ai_describe.html", _ctx(request, text=text, error=str(exc),
                                                              blueprint_json=json.dumps(blueprint, indent=1)))
    engine.audit(request.workspace, "workflow.ai_built", f"Built “{wf.name}” from a description", actor=request.user,
                 workflow=wf, request=request)
    if wf.is_published:
        messages.success(request, f"“{wf.name}” is built and published. Review it, then share it from Add autostart.")
    elif problems:
        messages.warning(request, f"“{wf.name}” is built as a draft. Before publishing: {problems[0]}")
    else:
        messages.success(request, f"“{wf.name}” is built. Review it and click Publish.")
    return redirect("flows:builder", wf.pk)


# ---- Builder › AI assistant (side panel): improve the workflow being edited ---------------------------------------
def _plan_key(wf):
    return f"ai_plan_{wf.pk}"


def _has_model(ws):
    from .. import ai_actions

    cfg = ai_actions.settings_for(ws)
    if not ws.feature("ai") or cfg.get("disabled"):
        return False
    if cfg.get("engine"):
        return True
    return ai_providers.default_engine() != ai_providers.PLANNER


def _engine_label(ws):
    from .. import ai_actions

    try:
        engine_id = ai_actions.engine_for(ws)
    except ai_actions.AIError:
        return ""
    with ai_providers.bind(ai_actions.settings_for(ws)):
        return ai_providers.label(engine_id)


def _panel(request, wf, **extra):
    from .. import ai_actions, ai_workflow

    return render(request, "flows/partials/ai_panel.html", {
        "engine_label": _engine_label(wf.workspace),
        "wf": wf, "has_models": _has_model(wf.workspace), "ai_on": ai_actions.enabled(wf.workspace),
        "undo": (wf.options or {}).get("ai_undo"),
        "plan": request.session.get(_plan_key(wf)), "suggestions": request.session.get(f"ai_suggest_{wf.pk}"),
        "examples": ai_workflow.examples(wf),
        **extra})


@login_required
def workflow_assistant(request, pk):
    """GET (fetch) → the panel; POST do=suggest|plan|discard (fetch) → the panel; do=apply|undo → back to the builder."""
    from django.core.exceptions import PermissionDenied
    from django.urls import reverse

    from .. import ai_actions, ai_workflow
    from .builder import _editable

    wf = _editable(request, pk)
    ws = request.workspace
    back = redirect(reverse("flows:builder", args=[wf.pk]) + "?ai=1")
    if request.method != "POST":
        return _panel(request, wf) if request.headers.get("X-Requested-With") == "fetch" or request.GET.get("panel") else back
    do = request.POST.get("do")
    if do == "undo":
        if ai_workflow.undo(wf):
            engine.audit(ws, "workflow.ai_undo", wf.name, actor=request.user, workflow=wf, request=request)
            messages.success(request, "The AI change was undone.")
        return back
    if do == "discard":
        request.session.pop(_plan_key(wf), None)
        return _panel(request, wf)
    if not ai_actions.enabled(ws):
        raise PermissionDenied("AI is turned off for this workspace.")
    text = request.POST.get("text", "").strip()[:MAX_TEXT]
    engine_id = ""  # the workspace model; a posted engine is ignored
    try:
        if do == "explain":
            ex = ai_workflow.explain(wf, request.user, refresh=request.POST.get("refresh") == "1")
            return _panel(request, wf, explanation=ex, explanation_text=ai_workflow.explain_text(ex, wf.name))
        if do == "suggest":
            request.session[f"ai_suggest_{wf.pk}"] = ai_workflow.suggest(wf, request.user, engine_id)
        elif do in ("plan", "refine"):
            if not text:
                raise ai_actions.AIError("Describe the change you want.")
            started = time.monotonic()
            pending = request.session.get(_plan_key(wf)) if do == "refine" else None
            if pending:
                r = ai_workflow.propose(wf, text, request.user, engine_id, start=pending["blueprint"], earlier=pending["text"])
                text = f"{pending['text']} → {text}"[:MAX_TEXT]
            else:
                r = ai_workflow.propose(wf, text, request.user, engine_id)
            request.session[_plan_key(wf)] = {
                "blueprint": r["blueprint"], "text": text, "rounds": (pending or {}).get("rounds", 0) + 1, "changes": r["changes"], "diff": r["diff"],
                "problems": r["preview"]["problems"], "engine_label": r["engine_label"],
                "new_warnings": r["preview"].get("new_warnings", []),
                "seconds": round(time.monotonic() - started, 1),
                "counts": [r["preview"]["step_count"], r["preview"]["field_count"], len(r["preview"]["bots"])]}
        elif do == "apply":
            plan = request.session.pop(_plan_key(wf), None)
            if not plan:
                raise ai_actions.AIError("That proposal expired — ask again.")
            problems = ai_workflow.apply(wf, plan["blueprint"], request.user, plan["text"])
            request.session.pop(f"ai_suggest_{wf.pk}", None)
            engine.audit(ws, "workflow.ai_edit", plan["text"][:200], actor=request.user, workflow=wf, request=request)
            if problems:
                messages.warning(request, f"AI change applied (not published). Before publishing: {problems[0]}")
            else:
                messages.success(request, "AI change applied. Review it, then Publish — or undo it with ↶ (⌘Z).")
            return back
    except (ai_actions.AIError, BlueprintError) as exc:
        if do == "apply":
            messages.error(request, str(exc))
            return back
        return _panel(request, wf, error=str(exc), text=text)
    return _panel(request, wf, text="" if do in ("plan", "refine") else text)


@login_required
def workflow_assistant_after(request, pk):
    """The builder canvas for the pending AI proposal (JSON: html, marks, removed)."""
    from django.http import JsonResponse

    from .. import ai_workflow
    from ..permissions import get_workflow

    wf = get_workflow(request, pk, edit=True)
    plan = request.session.get(_plan_key(wf))
    if not plan:
        return JsonResponse({"error": "No proposal"}, status=404)
    try:
        return JsonResponse(ai_workflow.render_after(request, wf, plan))
    except BlueprintError as exc:
        return JsonResponse({"error": str(exc)}, status=400)


# ---- Documents › ✦ plain-English search ------------------------------------------------------------------------
@login_required
def document_search(request):
    from urllib.parse import urlencode

    from django.urls import reverse

    from .. import ai_actions
    from ..models import Tag, Workflow

    q = request.GET.get("q", "").strip()[:300]
    if not q:
        return redirect("flows:documents")
    try:
        if not ai_actions.enabled(request.workspace, "nl_search"):
            raise ai_actions.AIError("AI search is turned off.")
        filters = ai_actions.search_filters(
            request.workspace, q, Workflow.objects.visible_to(request.membership).exclude(kind="document"),
            Tag.objects.filter(workspace=request.workspace), user=request.user)
    except ai_actions.AIError as exc:
        messages.warning(request, f"{exc} Searching by text instead.")
        filters = {"q": q}
    if not filters:
        filters = {"q": q}
    request.session["ai_search"] = q
    return redirect(reverse("flows:documents") + "?" + urlencode({**filters, "ai": q}))


# ---- Document › AI summary ------------------------------------------------------------------------------------
@login_required
def document_summary(request, pk):
    from django.http import JsonResponse
    from django.shortcuts import get_object_or_404

    from .. import ai_actions
    from ..models import Packet

    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)
    packet = get_object_or_404(Packet.objects.visible_to(request.membership), pk=pk)
    if not ai_actions.enabled(packet.workspace, "ask_ai"):
        return JsonResponse({"error": "AI is turned off for this workspace."}, status=403)
    try:
        s = ai_actions.cached_summary(packet, request.user, refresh=request.POST.get("refresh") == "1")
    except ai_actions.AIError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"text": s["text"], "engine": s.get("engine", ""), "at": s["at"]})
