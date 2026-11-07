"""Workflow builder (diagram), step settings drawer, autostart, documents panel and bots."""
import json
import zoneinfo

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import Connection, Contact, Group

from ..sla import ESCALATIONS as SLA_ESCALATIONS, UNITS as SLA_UNITS
from .. import diagram, engine, mailer, services
from ..bots import CATALOG, CATEGORY_TREE, bot_meta
from ..fieldtypes import CONDITION_OPERATORS, condition_label
from ..models import AccessRequest, Bot, DataSheet, Step, Workflow, WorkflowDocument
from ..permissions import get_workflow
from core.utils import safe_next


def _editable(request, pk):
    wf = get_workflow(request, pk, edit=True)
    holder = wf.lock_holder(request.user)
    if holder is not None:
        raise PermissionDenied(f"{holder.get_full_name() or holder.email} is configuring this workflow right now.")
    wf.locked_by = request.user
    wf.locked_at = timezone.now()
    wf.save(update_fields=["locked_by", "locked_at"])
    from .. import undo

    label = undo.label_for(request)
    if label:  # every builder change can be undone
        undo.record(wf, request.user, label)
    return wf


def _back(wf):
    return redirect("flows:builder", wf.pk)


def _jumps_from_post(post):
    """Conditional "jump from this step" links: [{"to", "logic", "rules"}]."""
    out = []
    for i in range(int(post.get("jump_count", 0) or 0)):
        if post.get(f"j{i}_delete") == "on" or not post.get(f"j{i}_to"):
            continue
        cond = _rules_from_post(post, f"j{i}")
        out.append({"to": post[f"j{i}_to"], "logic": cond["logic"], "rules": cond["rules"]})
    if post.get("add_jump") == "1":
        out.append({"to": "", "logic": "and", "rules": []})
    return out


def _rules_from_post(post, prefix="rule"):
    fields = post.getlist(f"{prefix}_field")
    ops = post.getlist(f"{prefix}_op")
    vals = post.getlist(f"{prefix}_value")
    rules = [{"field": f, "op": o, "value": v} for f, o, v in zip(fields, ops, vals) if f]
    return {"logic": post.get(f"{prefix}_logic", "and"), "rules": rules}


@login_required
def builder(request, pk):
    wf = get_workflow(request, pk, edit=True)
    holder = wf.lock_holder(request.user)
    if holder is not None and request.GET.get("takeover") != "1":
        return render(request, "flows/in_use.html", {"wf": wf, "holder": holder})
    if holder is not None:
        engine.notify(holder, wf.workspace, f"{engine.actor_label(request.user)} took over configuring {wf.name}",
                      "", reverse("flows:workflow_detail", args=[wf.pk]), kind="access_request")
    wf.locked_by = request.user
    wf.locked_at = timezone.now()
    wf.save(update_fields=["locked_by", "locked_at"])
    return render(request, "flows/builder.html", {
        **canvas_context(wf), "ai_enabled": __import__("flows.ai_actions", fromlist=["enabled"]).enabled(wf.workspace),
        "history": __import__("flows.undo", fromlist=["state"]).state(wf),
        "ai_open": request.GET.get("ai") == "1",
    })


def canvas_context(wf):
    """What the builder canvas (flows/partials/builder_flow.html) needs — also used for the AI "after" preview."""
    steps = wf.ordered_steps()
    docs = list(wf.documents.all())
    bots = list(wf.bots.all())
    step_names = {s.key: s.name for s in steps}
    labels = {f["id"]: f.get("label") or f.get("name") or f["id"] for d in docs for f in d.fields}
    for s in steps:
        s.inline_bots = [b for b in bots if b.trigger == Bot.INLINE and b.step_key == s.key]
        s.attached_bots = [b for b in bots if b.step_key == s.key and b.trigger != Bot.INLINE]
        s.branch_view = [dict(b, target_name=step_names.get(b.get("target"), "Completed" if b.get("target") == "end" else "Next step"))
                         for b in (s.branches or [])]
        s.docs_to_fill = [d for d in docs if (s.document_access or {}).get(d.key, "fill") == "fill"]
        s.routes, s.step_bots = [], []
        for b in bots:
            if b.trigger == Bot.INLINE:
                continue
            acting = _acting_steps(b.condition, s)
            if b.bot_type == "route_to_step" and acting:
                s.routes.append({"bot": b, "target": step_names.get((b.config or {}).get("step"), "?"),
                                 "rules": _rule_summary(b.condition, labels, docs)})
            elif acting and b.step_key != s.key:
                s.step_bots.append(b)
    start_routes = [{"bot": b, "target": step_names.get((b.config or {}).get("step"), "?")}
                    for b in bots if b.bot_type == "route_to_step" and b.trigger == Bot.FLOW_START]
    from .. import autostart as auto_mod

    names = {o.pk: o.name for o in Workflow.objects.filter(workspace=wf.workspace)}
    autostarts = [{**i, "summary": auto_mod.describe(i, names)} for i in auto_mod.items(wf)]
    for s in steps:
        s.jump_view = [{"target": step_names.get(j.get("to"), "?"),
                        "rules": _rule_summary({"logic": j.get("logic", "and"), "rules": j.get("rules") or []}, labels, docs)}
                       for j in (s.settings or {}).get("jumps") or [] if j.get("to")]
    nodes = diagram.tree(steps)

    def annotate(nodes_):
        for n in nodes_:
            for ln in n["lanes"]:
                b = ln["branch"]
                target, then = b.get("target") or "next", b.get("then") or "merge"
                ln["is_lane"] = target == "lane"
                ln["enter"] = {"lane": "", "next": "Continue after the branches", "merge": "Continue after the branches",
                               "end": "Complete the workflow"}.get(target, f"Go to “{step_names.get(target, '?')}”")
                ln["exit"] = {"merge": "Continue after the branches", "end": "Complete the workflow"}.get(
                    then, f"Go to “{step_names.get(then, '?')}”")
                ln["rules_text"] = _rule_summary({"logic": b.get("logic", "and"), "rules": b.get("rules") or []}, labels, docs)
                ln["key"] = f"{n['step'].key}:{ln['id']}"
                annotate(ln["nodes"])
    annotate(nodes)
    from .. import checks

    findings = checks.run(wf)
    issues = checks.by_step(findings)
    live = step_activity(wf)
    for s in steps:
        s.live = live.get(s.key)
    for s in steps:
        s.issues = issues.get(s.key, [])
        s.issue_level = "error" if any(f["level"] == "error" for f in s.issues) else ("warning" if s.issues else "")
    from ..models import StepTemplate

    return {"checks": findings, "checks_summary": checks.summary(findings), "wf": wf, "steps": steps,
            "step_templates": list(StepTemplate.objects.filter(workspace=wf.workspace).only("id", "name")[:8]), "nodes": nodes, "docs": docs, "autostarts": autostarts, "bot_count": len(bots),
            "start_forms": wf.intake_forms.filter(archived_at__isnull=True), "start_routes": start_routes}


def step_activity(wf):
    """{step key: {"waiting", "overdue", "typical"}} for documents in progress, plus how long the step usually takes
    (median of its last 200 completions). Two queries; empty for a workflow nobody has run."""
    from statistics import median

    from django.db.models import Count, Q

    from ..models import Packet, StepRun

    if not wf.pk or not wf.packets.exists():
        return {}
    now = timezone.now()
    open_runs = (StepRun.objects.filter(packet__workflow=wf, packet__status=Packet.IN_PROGRESS, packet__trashed_at__isnull=True,
                                        packet__is_test=False, status__in=[StepRun.PENDING, StepRun.OPENED])
                 .values("step_key").annotate(n=Count("packet", distinct=True),
                                              late=Count("packet", distinct=True, filter=Q(due_at__lt=now))))
    out = {r["step_key"]: {"waiting": r["n"], "overdue": r["late"], "typical": ""} for r in open_runs}
    done = (StepRun.objects.filter(packet__workflow=wf, packet__is_test=False, status=StepRun.COMPLETED,
                                   completed_at__isnull=False).order_by("-completed_at")
            .values_list("step_key", "created_at", "completed_at")[:200])
    spans = {}
    for key, start, end in done:
        spans.setdefault(key, []).append((end - start).total_seconds())
    for key, vals in spans.items():
        if len(vals) >= 3:
            h = median(vals) / 3600
            text = f"{h * 60:.0f} min" if h < 1 else (f"{h:.1f} h" if h < 24 else f"{h / 24:.1f} days")
            out.setdefault(key, {"waiting": 0, "overdue": 0, "typical": ""})["typical"] = text
    return out


def _acting_steps(condition, step):
    """Does a bot condition reference ``step`` (by key or imported alias) as the acting step?"""
    ids = {step.key, *((step.settings or {}).get("aliases") or [])}
    found = []

    def visit(rule):
        for r in (rule or {}).get("rules", []):
            if "rules" in r:
                visit(r)
            elif r.get("field") == "@step" and r.get("value") in ids:
                found.append(r)
    visit(condition)
    return bool(found)


def _rule_summary(condition, fields=None, docs=()):
    parts = []

    def visit(rule, depth=0):
        bits = []
        for r in (rule or {}).get("rules", []):
            if "rules" in r:
                inner = visit(r, depth + 1)
                if inner:
                    bits.append(f"({inner})" if depth else inner)
            elif not str(r.get("field", "")).startswith("@") or condition_label(r["field"], docs) or str(r["field"]).startswith(("@any:", "@all:")):
                op = {"eq": "=", "neq": "≠", "gt": ">", "lt": "<", "gte": "≥", "lte": "≤"}.get(r.get("op"), r.get("op", "").replace("_", " "))
                if str(r["field"]).startswith(("@any:", "@all:")):
                    names = [x.strip() for x in r["field"][5:].split(",")]
                    name = ("Any of " if r["field"].startswith("@any:") else "All of ") + ", ".join((fields or {}).get(x, x) for x in names)
                else:
                    name = condition_label(r["field"], docs) or (fields or {}).get(r["field"], r["field"])
                bits.append(f"{name} {op} {r.get('value') or ''}".strip())
        return f" {(rule or {}).get('logic', 'and').upper()} ".join(b for b in bits if b)
    parts.append(visit(condition))
    return " ".join(p for p in parts if p)


@login_required
@require_POST
def builder_history(request, pk, action):
    direction = action
    """Undo or redo the latest builder change (not published until the user publishes)."""
    from .. import undo

    wf = _editable(request, pk)
    label = undo.undo(wf, request.user) if direction == "undo" else undo.redo(wf, request.user) if direction == "redo" else ""
    if label:
        engine.audit(wf.workspace, f"workflow.{direction}", label, actor=request.user, workflow=wf, request=request)
        messages.success(request, f"{'Undone' if direction == 'undo' else 'Redone'}: {label}.")
    else:
        messages.info(request, f"Nothing to {direction}.")
    return _back(wf)


_EMAIL = r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+"


def workflow_emails(wf):
    """Every email address written into the workflow's steps and bots, with how often it appears."""
    import re

    found = {}
    blobs = [json.dumps([s.recipient_email, (s.settings or {}).get("notify_emails"), ((s.settings or {}).get("sla") or {}).get("escalate_to")])
             for s in wf.steps.all()] + [json.dumps(b.config or {}) for b in wf.bots.all()]
    for blob in blobs:
        for e in re.findall(_EMAIL, blob):
            found[e.lower()] = found.get(e.lower(), 0) + 1
    return sorted(found.items())


@login_required
def replace_recipient(request, pk):
    """Swap one email address for another in every step and bot of this workflow (e.g. when someone leaves)."""
    import re

    if request.method != "POST":
        wf = get_workflow(request, pk, edit=True)
        return render(request, "flows/partials/replace_recipient.html", {"wf": wf, "emails": workflow_emails(wf)})
    wf = _editable(request, pk)
    old, new = (request.POST.get("old") or "").strip(), (request.POST.get("new") or "").strip()
    if not re.fullmatch(_EMAIL, old) or not re.fullmatch(_EMAIL, new):
        messages.error(request, "Enter two email addresses.")
        return _back(wf)
    pattern = re.compile(rf"(?<![\w.+'-]){re.escape(old)}(?![\w-])", re.I)
    swap = lambda v: json.loads(pattern.sub(new.replace("\\", "\\\\"), json.dumps(v)))  # noqa: E731
    count = 0
    for st in wf.steps.all():
        changed = False
        for attr in ("recipient_email", "settings"):
            val = getattr(st, attr)
            out = swap(val)
            if out != val:
                setattr(st, attr, out)
                changed = True
        if changed:
            st.save()
            count += 1
    for b in wf.bots.all():
        out = swap(b.config or {})
        if out != (b.config or {}):
            b.config = out
            b.save(update_fields=["config"])
            count += 1
    if count:
        wf.touch()
        engine.audit(wf.workspace, "workflow.recipient_replaced", f"{old} → {new} in {count} place{'s' if count != 1 else ''}",
                     actor=request.user, workflow=wf, request=request)
        messages.success(request, f"Replaced {old} with {new} in {count} step{'s' if count != 1 else ''} and bot{'s' if count != 1 else ''}. Publish to make it live.")
    else:
        messages.info(request, f"{old} isn't used in this workflow.")
    return _back(wf)


@login_required
@require_POST
def ai_step_email(request, pk, step_id):
    """✦ Write with AI: an invitation subject and message for this step, in the chosen tone (nothing is saved)."""
    from django.http import JsonResponse

    from .. import ai_actions

    wf = get_workflow(request, pk, edit=True)
    step = get_object_or_404(Step, workflow=wf, pk=step_id)
    if not ai_actions.enabled(wf.workspace):
        return JsonResponse({"error": "AI is turned off for this workspace."}, status=403)
    try:
        return JsonResponse(ai_actions.draft_invitation(wf, step, request.POST.get("tone", "friendly"), user=request.user))
    except ai_actions.AIError as exc:
        return JsonResponse({"error": str(exc)}, status=400)


@login_required
def key_dates_settings(request, pk):
    """Modal: which date fields are key dates, when to remind and who else to tell."""
    from .. import keydates

    wf = get_workflow(request, pk, edit=True)
    fields = keydates.date_fields(wf)
    if request.method == "POST":
        p = request.POST
        cfg = [{"field": fid, "label": (p.get(f"label_{fid}") or label)[:160], "remind": keydates.clean_days(p.get(f"days_{fid}")),
                "emails": ", ".join(e.strip() for e in p.get(f"emails_{fid}", "").split(",") if "@" in e)[:500]}
               for fid, label, _s in fields if p.get(f"on_{fid}") == "on"]
        wf.options = {**(wf.options or {}), "key_dates": cfg}
        wf.save(update_fields=["options"])
        found = keydates.backfill(wf) if cfg else 0
        engine.audit(wf.workspace, "workflow.key_dates", ", ".join(c["label"] for c in cfg) or "off", actor=request.user, workflow=wf, request=request)
        messages.success(request, (f"Tracking {len(cfg)} key date{'s' if len(cfg) != 1 else ''}"
                                   + (f"; {found} finished document{'s' if found != 1 else ''} already have them." if found else ".")) if cfg
                         else "Key dates turned off for this workflow.")
        return redirect(request.POST.get("next") or wf.get_absolute_url())
    current = {c["field"]: c for c in keydates.config(wf)}
    rows = [{"id": fid, "label": label, "suggested": sug, "on": fid in current, "cfg": current.get(fid) or {},
             "days": ", ".join(str(d) for d in (current.get(fid) or {}).get("remind", keydates.DEFAULT_REMIND))}
            for fid, label, sug in fields]
    return render(request, "flows/partials/key_dates_settings.html", {"wf": wf, "rows": rows,
                                                                      "next": request.GET.get("next", "")})


@login_required
def step_templates_panel(request, pk):
    """Modal: the workspace's step templates, with delete."""
    from .. import step_templates
    from ..models import StepTemplate

    wf = get_workflow(request, pk, edit=True)
    if request.method == "POST":
        StepTemplate.objects.filter(workspace=wf.workspace, pk=request.POST.get("delete") or 0).delete()
        messages.success(request, "Step template deleted.")
        return _back(wf)
    rows = list(StepTemplate.objects.filter(workspace=wf.workspace))
    for t in rows:
        t.summary = step_templates.summary(t)
    return render(request, "flows/partials/step_templates.html", {"wf": wf, "templates": rows})


@login_required
def workflow_checks(request, pk):
    """Side panel: every check finding, with a link to the step and an "Ask AI to fix" request."""
    from .. import ai_actions, checks

    wf = get_workflow(request, pk, edit=True)
    findings = checks.run(wf)
    keys = {s.key: s for s in wf.steps.all()}
    for f in findings:
        f["step_obj"] = keys.get(f["step"])
    fixes = [f["fix"] for f in findings if f.get("fix") and f["level"] != checks.INFO]
    fix_all = ("Fix these problems, changing nothing else: " + "; ".join(dict.fromkeys(fixes))) if len(fixes) > 1 else ""
    return render(request, "flows/partials/checks_panel.html", {
        "wf": wf, "findings": findings, "summary": checks.summary(findings), "fix_all": fix_all[:3500],
        "ai_enabled": ai_actions.enabled(wf.workspace)})


@login_required
@require_POST
def request_access(request, pk):
    wf = get_workflow(request, pk)
    holder = wf.lock_holder(request.user)
    AccessRequest.objects.create(workflow=wf, requester=request.user)
    if holder:
        link = reverse("flows:builder", args=[wf.pk])
        engine.notify(holder, wf.workspace, f"{engine.actor_label(request.user)} requests access to configure {wf.name}",
                      "Finish your changes and release the workflow.", link, kind="access_request")
        mailer.send(wf.workspace, holder.email, "access_request", {
            "user_name": engine.actor_label(holder), "actor_name": engine.actor_label(request.user),
            "workflow_name": wf.name, "link": engine.absolute(link)})
    messages.success(request, "Your request was sent.")
    return redirect(wf)


@login_required
@require_POST
def finish_later(request, pk):
    wf = get_workflow(request, pk, edit=True)
    if wf.locked_by_id == request.user.id:
        wf.locked_by = None
        wf.locked_at = None
        wf.save(update_fields=["locked_by", "locked_at"])
    messages.info(request, "Your changes are saved. The workflow is not published yet." if wf.has_unpublished_changes else "Saved.")
    return redirect(wf)


@login_required
@require_POST
def publish(request, pk):
    wf = _editable(request, pk)
    problems = validate(wf)
    if problems:
        for p in problems:
            messages.error(request, p)
        return _back(wf)
    from .. import checks

    warnings = [f for f in checks.run(wf) if f["level"] == checks.WARNING]
    note = (request.POST.get("note") or "").strip()[:300]
    services.publish(wf, request.user)
    if warnings:
        messages.warning(request, f"Published with {len(warnings)} warning{'s' if len(warnings) != 1 else ''}: "
                                  f"{warnings[0]['message']} Open Checks in the builder to see them all.")
    wf.locked_by = None
    wf.locked_at = None
    wf.save(update_fields=["locked_by", "locked_at"])
    engine.audit(wf.workspace, "workflow.published", f"Version {wf.version}" + (f": {note}" if note else ""),
                 actor=request.user, workflow=wf, request=request)
    messages.success(request, f"“{wf.name}” is published (version {wf.version}).")
    return redirect(wf)


@login_required
@require_POST
def discard_draft(request, pk):
    """Throw away unpublished changes: the editor goes back to the live version (undo brings them back)."""
    wf = _editable(request, pk)
    if wf.status != Workflow.PUBLISHED or not wf.published_snapshot:
        messages.error(request, "This workflow has never been published, so there is nothing to go back to.")
        return _back(wf)
    services.load_snapshot(wf, wf.published_snapshot)
    wf.has_unpublished_changes = False
    wf.save(update_fields=["has_unpublished_changes"])
    engine.audit(wf.workspace, "workflow.discarded", f"Back to version {wf.version}", actor=request.user, workflow=wf, request=request)
    messages.success(request, f"Changes discarded: the editor is back to version {wf.version}. Undo with ↶ if you need them.")
    return _back(wf)


@login_required
def publish_review(request, pk):
    """Modal shown before Publish: what changes since the live version, the checks, and who is affected."""
    from .. import checks, versions
    from ..models import Packet

    wf = get_workflow(request, pk, edit=True)
    live = wf.published_snapshot if wf.status == Workflow.PUBLISHED else None
    changes = versions.diff(live or {}, wf.build_snapshot()) if live else []
    findings = checks.run(wf)
    return render(request, "flows/partials/publish_review.html", {
        "wf": wf, "live": live, "changes": changes[:40], "more": max(len(changes) - 40, 0),
        "errors": [f for f in findings if f["level"] == checks.ERROR],
        "warnings": [f for f in findings if f["level"] == checks.WARNING],
        "in_progress": wf.packets.filter(status=Packet.IN_PROGRESS, trashed_at__isnull=True).count(),
        "next_version": wf.version + 1 if live else wf.version})


def validate(wf):
    """The problems that block Publish (the error-level workflow checks)."""
    from .. import checks

    return checks.errors(checks.run(wf))


# ---- steps -------------------------------------------------------------------------------
@login_required
@require_POST
def step_add(request, pk):
    wf = _editable(request, pk)
    kind = request.POST.get("kind", Step.RECIPIENT)
    after = int(request.POST.get("after", 0) or 0)
    if kind == "approval":
        return _add_approval(request, wf, after)
    if kind == "template":
        from .. import step_templates
        from ..models import StepTemplate

        tpl = get_object_or_404(StepTemplate, workspace=wf.workspace, pk=request.POST.get("template") or 0)
        lane, lane_settings = request.POST.get("lane", ""), None
        if ":" in lane:
            bkey, lid = lane.split(":", 1)
            if wf.steps.filter(key=bkey, kind=Step.BRANCH).exists():
                lane_settings = {"branch": bkey, "lane": lid}
        step = step_templates.insert(tpl, wf, after=after, lane=lane_settings)
        services.renumber_steps(wf)
        wf.touch()
        messages.success(request, f"Added “{step.name}” from your step templates.")
        return redirect(reverse("flows:builder", args=[wf.pk]) + f"?open={step.pk}")
    if kind not in (Step.RECIPIENT, Step.BRANCH, Step.BOT):
        messages.error(request, "Choose what to add.")
        return _back(wf)
    n = wf.steps.filter(kind=Step.RECIPIENT).count() + 1
    defaults = {
        Step.RECIPIENT: dict(name=f"Recipient {n}"),
        Step.BRANCH: dict(name="Branches", branches=[diagram.new_lane("Branch 1"), diagram.new_lane("Branch 2")]),
        Step.BOT: dict(name="Bot"),
    }[kind]
    lane = request.POST.get("lane", "")
    if ":" in lane:  # add inside a branch lane: "<branch key>:<lane id>"
        bkey, lid = lane.split(":", 1)
        if wf.steps.filter(key=bkey, kind=Step.BRANCH).exists():
            defaults["settings"] = {"lane": {"branch": bkey, "lane": lid}}
    step = Step.objects.create(workflow=wf, kind=kind, order=after * 1 + 5, **defaults)
    services.renumber_steps(wf)
    wf.touch()
    if kind == Step.BOT:
        return redirect(reverse("flows:bot_catalog", args=[wf.pk]) + f"?trigger=inline&step={step.key}")
    return redirect(reverse("flows:builder", args=[wf.pk]) + f"?open={step.pk}")


def _add_approval(request, wf, after):
    """One click: an approver step, a required Approve / Reject field it fills (plus comments), and a branch right after
    it that completes the workflow when the answer is Reject."""
    from ..models import new_key

    doc = wf.documents.filter(kind=WorkflowDocument.FORM).order_by("order", "id").first()
    if doc is None:
        messages.error(request, "Add a form first: the approval needs somewhere to record the decision.")
        return _back(wf)
    n = wf.steps.filter(kind=Step.RECIPIENT, name__startswith="Approval").count() + 1
    name = "Approval" if n == 1 else f"Approval {n}"
    lane = request.POST.get("lane", "")
    lane_settings = {}
    if ":" in lane:
        bkey, lid = lane.split(":", 1)
        if wf.steps.filter(key=bkey, kind=Step.BRANCH).exists():
            lane_settings = {"lane": {"branch": bkey, "lane": lid}}
    step = Step.objects.create(workflow=wf, kind=Step.RECIPIENT, name=name, order=after + 5,
                               message_subject="Please review: {workflow_name}", settings=dict(lane_settings))
    taken = {str(f.get("name") or "").lower() for f in doc.fields}
    label = f"{name} decision"
    while label.lower() in taken:
        label += " (2)"
    decision = {"id": new_key(), "type": "radio", "label": label, "name": label, "required": True, "role": step.key,
                "options": ["Approve", "Reject"]}
    comments = {"id": new_key(), "type": "textarea", "label": f"{name} comments", "name": f"{name} comments",
                "required": False, "role": step.key}
    doc.schema = {**(doc.schema or {}), "fields": [*doc.fields, {"id": new_key(), "type": "heading", "label": name,
                                                                 "text": name}, decision, comments]}
    doc.save(update_fields=["schema"])
    reject = {**diagram.new_lane("Rejected"), "rules": [{"field": label, "op": "eq", "value": "Reject"}], "target": "end"}
    Step.objects.create(workflow=wf, kind=Step.BRANCH, name=f"{name}: rejected?", order=after + 6, branches=[reject],
                        settings=dict(lane_settings))
    services.renumber_steps(wf)
    wf.touch()
    messages.success(request, f"Added “{name}”: choose who approves, and the workflow ends when they reject.")
    return redirect(reverse("flows:builder", args=[wf.pk]) + f"?open={step.pk}")


@login_required
@require_POST
def step_action(request, pk, step_id, action):
    wf = _editable(request, pk)
    step = get_object_or_404(Step, workflow=wf, pk=step_id)
    steps = wf.ordered_steps()
    idx = steps.index(step)
    if action == "delete":
        if step.kind == Step.RECIPIENT and wf.steps.filter(kind=Step.RECIPIENT).count() == 1:
            messages.error(request, "A workflow needs at least one recipient step.")
            return _back(wf)
        doomed = {step.key} | (diagram.descendants(steps, step.key) if step.kind == Step.BRANCH else set())
        if not wf.steps.filter(kind=Step.RECIPIENT).exclude(key__in=doomed).exists():
            messages.error(request, "A workflow needs at least one recipient step outside this branch.")
            return _back(wf)
        wf.bots.filter(step_key__in=doomed).delete()
        wf.steps.filter(key__in=doomed).delete()
    elif action in ("up", "down"):
        sibs = diagram.children(steps, diagram.lane_of(step))
        i = sibs.index(step)
        j = i - 1 if action == "up" else i + 1
        if 0 <= j < len(sibs):
            sibs[i].order, sibs[j].order = sibs[j].order, sibs[i].order
            sibs[i].save(update_fields=["order"])
            sibs[j].save(update_fields=["order"])
    elif action == "duplicate":
        clone = Step.objects.get(pk=step.pk)
        clone.pk = None
        clone.key = services.new_key()
        clone.name = f"{step.name} (copy)"
        clone.order = step.order + 5
        clone.save()
    elif action == "rename":
        step.name = (request.POST.get("name") or step.name)[:120]
        step.save(update_fields=["name"])
    elif action == "save_template":
        from .. import step_templates

        if step.kind != Step.RECIPIENT:
            messages.error(request, "Only recipient steps can be saved as templates.")
            return _back(wf)
        tpl = step_templates.save(step, request.user, request.POST.get("name") or step.name)
        messages.success(request, f"Saved “{tpl.name}” as a step template. Add it to any workflow from the + menu.")
        return _back(wf)
    services.renumber_steps(wf)
    wf.touch()
    return _back(wf)


@login_required
def step_settings(request, pk, step_id):
    wf = _editable(request, pk)
    step = get_object_or_404(Step, workflow=wf, pk=step_id)
    docs = list(wf.documents.all())
    if request.method == "POST":
        p = request.POST
        step.name = (p.get("name") or step.name)[:120]
        if step.kind == Step.RECIPIENT:
            step.recipient_type = p.get("recipient_type", step.recipient_type)
            step.recipient_email = p.get("recipient_email", "").strip()
            step.recipient_name = p.get("recipient_name", "").strip()
            step.recipient_contact = Contact.objects.filter(workspace=wf.workspace, pk=p.get("recipient_contact") or 0).first()
            step.recipient_group = Group.objects.filter(workspace=wf.workspace, pk=p.get("recipient_group") or 0).first()
            step.message_subject = p.get("message_subject", step.message_subject)[:200]
            step.message_body = p.get("message_body", step.message_body)
            step.attach_documents = p.get("attach_documents") == "on"
            step.document_access = {d.key: p.get(f"access_{d.key}", "fill") for d in docs}
            step.revoke_on_complete = p.get("revoke_on_complete") == "on"
            step.thank_you = {"title": p.get("ty_title", ""), "body": p.get("ty_body", "")}
            step.condition = _rules_from_post(p)
            step.settings = {
                "redirect_url": p.get("redirect_url", "").strip() if p.get("redirect_on") == "on" else "",
                "tags": [t.strip() for t in p.get("tags", "").split(",") if t.strip()] if p.get("tags_on") == "on" else [],
                "reminder": {"enabled": p.get("reminder_on") == "on", "every_days": p.get("reminder_days") or 1,
                             "max": p.get("reminder_max") or 3},
                "expiration": {"enabled": p.get("expiration_on") == "on", "days": p.get("expiration_days") or 7},
                "sla": {"amount": p.get("sla_amount") or 2, "unit": p.get("sla_unit") or "business_days",
                        "escalate": p.get("sla_escalate") or "notify", "escalate_to": p.get("sla_escalate_to", "").strip(),
                        "tag": p.get("sla_tag", "").strip()} if p.get("sla_on") == "on" else {},
                "notify_emails": p.get("notify_emails", "").strip() if p.get("notify_on") == "on" else "",
                "forbid_decline": p.get("forbid_decline") == "on",
                "one_tap": p.get("one_tap") == "on" if p.get("one_tap_present") else (step.settings or {}).get("one_tap", True),
                "forbid_discard": p.get("forbid_discard") == "on",
                "forbid_draft": p.get("forbid_draft") == "on",
                "recurrence": p.get("recurrence") == "on",
                "auto_complete": p.get("auto_complete") == "on",
                "aliases": [a.strip() for a in p.get("aliases", "").split(",") if a.strip()],
                "verify": ({"method": p.get("verify_method"), "phone": p.get("verify_phone", "").strip()[:200],
                            "code": p.get("verify_code", "").strip()[:200], "connection": p.get("verify_connection") or ""}
                           if p.get("verify_method") in ("email", "sms", "code") else {}),
                # keep structure the form does not edit (branch-lane membership, import ids)
                **{k: v for k, v in (step.settings or {}).items() if k in ("source_id", "lane")},
                "jumps": _jumps_from_post(p),
            }
            # reassign fields to this step
            for d in docs:
                ids = set(p.getlist(f"fields_{d.key}"))
                if f"fields_present_{d.key}" not in p:
                    continue
                changed = False
                for fld in d.fields:
                    if fld["id"] in ids and fld.get("role") != step.key:
                        fld["role"] = step.key
                        changed = True
                    elif fld["id"] not in ids and fld.get("role") == step.key:
                        fld["role"] = "anyone"
                        changed = True
                if changed:
                    d.save(update_fields=["schema"])
        elif step.kind == Step.BRANCH:
            branches, removed = [], []
            old = diagram.ensure_lane_ids([dict(x) for x in (step.branches or [])])
            for i in range(int(p.get("branch_count", 0))):
                lid = p.get(f"b{i}_id") or (old[i]["id"] if i < len(old) else f"i{i}")
                if p.get(f"b{i}_delete") == "on":
                    removed.append(lid)
                    continue
                cond = _rules_from_post(p, f"b{i}")
                branches.append({"id": lid, "label": p.get(f"b{i}_label") or f"Branch {i + 1}", "logic": cond["logic"],
                                 "rules": cond["rules"], "target": p.get(f"b{i}_target", "lane"),
                                 "then": p.get(f"b{i}_then", "merge")})
            if p.get("add_branch") == "1":
                branches.append(diagram.new_lane(f"Branch {len(branches) + 1}"))
            if removed:  # a deleted lane takes its steps (and anything nested in them) with it
                all_steps = wf.ordered_steps()
                doomed = set()
                for x in all_steps:
                    if diagram.lane_of(x) in [(step.key, r) for r in removed]:
                        doomed |= {x.key} | diagram.descendants(all_steps, x.key)
                wf.bots.filter(step_key__in=doomed).delete()
                wf.steps.filter(key__in=doomed).delete()
            step.branches = branches
        step.save()
        wf.touch()
        if p.get("add_branch") == "1" or p.get("add_jump") == "1":
            return redirect(reverse("flows:builder", args=[wf.pk]) + f"?open={step.pk}")
        messages.success(request, f"“{step.name}” settings applied.")
        return _back(wf)
    fields = services.field_choices(wf)
    return render(request, "flows/partials/step_settings.html", {
        "wf": wf, "step": step, "docs": docs, "fields": fields, "ops": CONDITION_OPERATORS,
        "contacts": Contact.objects.filter(workspace=wf.workspace), "groups": Group.objects.filter(workspace=wf.workspace),
        "connections": Connection.objects.filter(workspace=wf.workspace),
        "other_steps": [s for s in wf.ordered_steps() if s.pk != step.pk and s.kind == Step.RECIPIENT],
        "rules": (step.condition or {}).get("rules") or [{}], "logic": (step.condition or {}).get("logic", "and"),
        "s": step.settings or {}, "sla_units": SLA_UNITS, "sla_escalations": SLA_ESCALATIONS,
        "ai_enabled": __import__("flows.ai_actions", fromlist=["enabled"]).enabled(wf.workspace),
        "branch_view": [dict(b, prefix=f"b{i}", rules_or_blank=b.get("rules") or [{}],
                             lane_steps=len(diagram.children(wf.ordered_steps(), (step.key, b["id"]))))
                        for i, b in enumerate(diagram.ensure_lane_ids([dict(x) for x in (step.branches or [])]))],
        "all_steps": [x for x in wf.ordered_steps() if x.pk != step.pk and x.kind in (Step.RECIPIENT, Step.BRANCH)],
        "jump_view": [dict(j, prefix=f"j{i}", rules_or_blank=j.get("rules") or [{}])
                      for i, j in enumerate((step.settings or {}).get("jumps") or [])],
    })


@login_required
def autostart(request, pk):
    """Add autostart panel: shareable link + a list of autostarts (scheduled, Excel rows, another workflow, events)."""
    from .. import autostart as auto_mod

    wf = _editable(request, pk)
    its = auto_mod.items(wf)
    if request.method == "POST":
        p = request.POST
        action, item_id = p.get("action", "save"), p.get("item", "")
        if action == "delete":
            auto_mod.save_items(wf, [i for i in its if i.get("id") != item_id])
            wf.touch()
            messages.success(request, "Autostart removed.")
            return _back(wf)
        if action == "toggle":
            for i in its:
                if i.get("id") == item_id:
                    i["enabled"] = not i.get("enabled", True)
            auto_mod.save_items(wf, its)
            wf.touch()
            return _back(wf)
        item = next((i for i in its if i.get("id") == item_id), None)
        if item is None:
            kind = p.get("type", "")
            if kind not in auto_mod.TYPES:
                messages.error(request, "Choose how to start the workflow.")
                return _back(wf)
            item = {"id": auto_mod.new_id(), "type": kind, "enabled": True, "count": 0}
            its.append(item)
        error = _apply_autostart_form(wf, item, p, request.FILES)
        if error:
            messages.error(request, error)
            return _back(wf)
        auto_mod.save_items(wf, its)
        wf.touch()
        messages.success(request, f"{auto_mod.TYPES[item['type']]} saved.")
        return _back(wf)
    ctx = {
        "wf": wf, "items": its, "types": auto_mod.TYPES, "integrations": auto_mod.INTEGRATION_SOURCES,
        "others": Workflow.objects.visible_to(request.membership).live().exclude(pk=wf.pk).filter(kind="workflow"),
        "hook_url": engine.absolute(reverse("flows:hook", args=[wf.hook_token])),
        "link_url": engine.absolute(reverse("flows:public_start", args=[wf.link_token])),
        "weekdays": list(enumerate(auto_mod.WEEKDAYS)), "months": list(enumerate(auto_mod.MONTHS, 1)),
        "units": list(auto_mod.UNITS), "tz_default": settings.TIME_ZONE,
        "timezones": sorted(zoneinfo.available_timezones()),
        "source_names": {o.pk: o.name for o in Workflow.objects.filter(workspace=wf.workspace)},
        "inbound_domain": settings.PAPERLANE_INBOUND_DOMAIN, "inbound_ready": bool(settings.PAPERLANE_INBOUND_SECRET),
    }
    edit_id, add = request.GET.get("item"), request.GET.get("add")
    if edit_id or add:
        item = next((i for i in its if i.get("id") == edit_id), None) or {
            "type": add if add in auto_mod.TYPES else "webhook", "enabled": True, "repeat": "once",
            "schedule_type": "week", "months": list(range(1, 13)), "time": "09:00", "tz": settings.TIME_ZONE,
            "stop": "never", "unit": "hours", "every": 3, "row_mode": "all",
            "source": request.GET.get("source", ""), "senders": "members", "ai_fill": True,
            "start": timezone.localtime().strftime("%Y-%m-%dT%H:%M")}
        if item.get("type") == "sheet" and item.get("file"):
            try:
                item = {**item, "preview": auto_mod.read_sheet(item["file"])[:3]}
            except Exception:
                item = {**item, "preview": []}
        return render(request, "flows/partials/autostart_item.html", {**ctx, "item": item})
    return render(request, "flows/partials/autostart.html", {
        **ctx, "rows": [{**i, "label": auto_mod.TYPES.get(i["type"], i["type"]),
                         "summary": auto_mod.describe(i, ctx["source_names"])} for i in its],
        "tab": request.GET.get("tab", "autostart")})


def _apply_autostart_form(wf, item, p, files):
    """Copy the posted settings of one autostart onto ``item``. Returns an error message or ''."""
    from .. import autostart as auto_mod

    t = item["type"]
    item["notify_admin"] = p.get("notify_admin") == "on"
    if t in ("schedule", "sheet"):
        item.update(start=p.get("start", "").strip(), tz=p.get("tz") or settings.TIME_ZONE,
                    repeat=p.get("repeat", "once"), time=p.get("time", "09:00") or "09:00",
                    schedule_type=p.get("schedule_type", "week"), unit=p.get("unit", "hours"),
                    months=[int(x) for x in p.getlist("months")] or list(range(1, 13)),
                    weekdays=[int(x) for x in p.getlist("weekdays")],
                    monthdays=sorted({int(x) for x in p.get("monthdays", "").replace(" ", "").split(",") if x.isdigit() and 1 <= int(x) <= 31}),
                    stop=p.get("stop", "never"), stop_date=p.get("stop_date", ""), stop_count=int(p.get("stop_count") or 0))
        try:
            item["every"] = max(1, min(999, int(p.get("every") or 1)))
        except ValueError:
            return "“Send documents every” must be a number."
        if item["repeat"] == "once" and not item["start"]:
            return "Choose the start date and time."
        if item["repeat"] == "schedule" and item["schedule_type"] == "week" and not item["weekdays"]:
            return "Pick at least one day of the week."
        if item["repeat"] == "schedule" and item["schedule_type"] == "month" and not item["monthdays"]:
            return "Enter the days of the month (e.g. 1, 15)."
        if item["stop"] == "date" and not item["stop_date"]:
            return "Choose the date to stop sending documents."
        if item["stop"] == "count" and item["stop_count"] < 1:
            return "Enter after how many runs to stop."
    if t == "sheet":
        item["row_mode"] = p.get("row_mode", "all")
        upload = files.get("sheet")
        if upload:
            if not upload.name.lower().endswith((".xlsx", ".xlsm", ".csv")):
                return "Upload an XLSX or CSV file."
            try:
                rows = auto_mod.read_sheet(upload, upload.name)
            except Exception as exc:
                return f"Could not read the file: {exc}"
            upload.seek(0)
            item.update(file=auto_mod.store_sheet(wf, upload), file_name=upload.name, rows=len(rows), next_row=0)
        if not item.get("file"):
            return "Upload the spreadsheet with one row per document."
    if t == "workflow":
        try:
            item["source_workflow"] = int(p.get("source_workflow") or 0)
        except ValueError:
            item["source_workflow"] = 0
        if not item["source_workflow"]:
            return "Choose the workflow that starts this one."
    if t == "webhook":
        item["source"] = p.get("source", "").strip()[:80]
    if t == "email":
        import secrets

        item.setdefault("token", secrets.token_hex(6))
        item["senders"] = p.get("senders") if p.get("senders") in ("members", "domains") else "members"
        item["domains"] = ", ".join(d.strip().lower().lstrip("@") for d in p.get("domains", "").replace(";", ",").split(",") if d.strip())[:300]
        item["ai_fill"] = p.get("ai_fill") == "on"
        if item["senders"] == "domains" and not item["domains"]:
            return "Enter the email domains that may start this workflow."
    return ""


# ---- documents panel ------------------------------------------------------------------------
@login_required
@require_POST
def doc_add(request, pk):
    wf = _editable(request, pk)
    upload = request.FILES.get("file")
    order = wf.documents.count()
    if upload:
        doc = WorkflowDocument.objects.create(
            workflow=wf, name=upload.name.rsplit(".", 1)[0][:200], kind=WorkflowDocument.FILE, file=upload, order=order,
            schema={"fields": [{"id": services.new_key(), "type": "description", "label": "Reference document",
                                "text": "Review the attached file, then complete the fields below."},
                               {"id": services.new_key(), "type": "signature", "label": "Signature", "name": "Signature",
                                "required": True, "role": "anyone"}]})
    else:
        doc = WorkflowDocument.objects.create(workflow=wf, name=request.POST.get("name") or "Untitled form",
                                              schema=services.blank_form_schema(), order=order)
    wf.touch()
    if request.POST.get("open") == "1":
        return redirect("flows:editor", wf.pk, doc.pk)
    return _back(wf)


@login_required
@require_POST
def doc_action(request, pk, doc_id, action):
    wf = _editable(request, pk)
    doc = get_object_or_404(WorkflowDocument, workflow=wf, pk=doc_id)
    if action == "rename":
        doc.name = (request.POST.get("name") or doc.name)[:200]
        doc.save(update_fields=["name"])
    elif action == "copy":
        clone = WorkflowDocument.objects.get(pk=doc.pk)
        clone.pk = None
        clone.key = services.new_key()
        clone.name = f"{doc.name} (copy)"
        clone.order = wf.documents.count()
        clone.save()
    elif action == "remove":
        if wf.documents.count() == 1:
            messages.error(request, "A workflow needs at least one document.")
            return _back(wf)
        doc.delete()
    elif action == "condition":
        doc.condition = _rules_from_post(request.POST)
        doc.save(update_fields=["condition"])
        messages.success(request, f"Conditions saved for “{doc.name}”.")
    elif action in ("up", "down"):
        docs = list(wf.documents.all())
        i = docs.index(doc)
        j = i - 1 if action == "up" else i + 1
        if 0 <= j < len(docs):
            docs[i], docs[j] = docs[j], docs[i]
            for n, d in enumerate(docs):
                d.order = n
                d.save(update_fields=["order"])
    wf.touch()
    return _back(wf)


@login_required
def doc_condition(request, pk, doc_id):
    wf = _editable(request, pk)
    doc = get_object_or_404(WorkflowDocument, workflow=wf, pk=doc_id)
    return render(request, "flows/partials/doc_condition.html", {
        "wf": wf, "doc": doc, "fields": services.field_choices(wf), "ops": CONDITION_OPERATORS,
        "rules": (doc.condition or {}).get("rules") or [{}], "logic": (doc.condition or {}).get("logic", "and")})


# ---- bots ------------------------------------------------------------------------------------
@login_required
def bots_page(request, pk):
    wf = _editable(request, pk)
    bots = list(wf.bots.all())
    steps = {s.key: s.name for s in wf.ordered_steps()}
    from django.db.models import Count, Max, Q

    from ..models import BotLog

    since = timezone.now() - timezone.timedelta(days=30)
    stats = {r["bot_key"]: r for r in BotLog.objects.filter(workflow=wf, created_at__gte=since).values("bot_key").annotate(
        runs=Count("id"), failed=Count("id", filter=Q(status="failed")), last=Max("created_at"))}
    last_status = {}
    for log in BotLog.objects.filter(workflow=wf, created_at__gte=since).order_by("bot_key", "-created_at").only(
            "bot_key", "status", "message"):
        last_status.setdefault(log.bot_key, log)
    for b in bots:
        b.meta = bot_meta(b.bot_type)
        b.step_name = steps.get(b.step_key, "Any step")
        b.stats = stats.get(b.key)
        b.last_log = last_status.get(b.key)
    groups = [(key, label, [b for b in bots if b.trigger == key]) for key, label in Bot.TRIGGERS]
    return render(request, "flows/bots.html", {"wf": wf, "groups": [g for g in groups if g[2]], "total": len(bots),
                                               "enabled": sum(b.enabled for b in bots),
                                               "failing": sum(1 for b in bots if b.stats and b.stats["failed"])})


@login_required
def bot_catalog(request, pk):
    wf = _editable(request, pk)
    q = request.GET.get("q", "").lower()
    cat = request.GET.get("cat", "Popular")
    items = []
    for key, meta in CATALOG.items():
        if q and q not in meta["name"].lower() and q not in meta["desc"].lower():
            continue
        if not q:
            if cat == "Popular" and not meta.get("popular"):
                continue
            if cat == "Free" and not meta.get("free"):
                continue
            if cat not in ("Popular", "All bots", "Free") and meta["category"] != cat:
                continue
        items.append((key, meta))
    return render(request, "flows/bot_catalog.html", {
        "wf": wf, "items": items, "cat": cat, "q": q, "tree": CATEGORY_TREE,
        "trigger": request.GET.get("trigger", ""), "step": request.GET.get("step", "")})


@login_required
def bot_edit(request, pk, bot_id=None):
    wf = _editable(request, pk)
    bot = get_object_or_404(Bot, workflow=wf, pk=bot_id) if bot_id else None
    bot_type = bot.bot_type if bot else request.GET.get("type") or request.POST.get("bot_type")
    if bot_type not in CATALOG:
        raise PermissionDenied("Unknown bot")
    meta = CATALOG[bot_type]
    if request.method == "POST":
        p = request.POST
        bot = bot or Bot(workflow=wf, bot_type=bot_type, order=wf.bots.count())
        bot.name = (p.get("name") or meta["name"])[:160]
        bot.trigger = p.get("trigger", Bot.STEP_COMPLETE)
        bot.step_key = p.get("step_key", "")
        bot.enabled = p.get("enabled") == "on"
        # Merge over the stored config: imported bots carry settings the form does not show (routing targets, recipients…).
        bot.config = {**(bot.config or {}),
                      **{f["name"]: (p.get(f"cfg_{f['name']}") == "on" if f["type"] == "checkbox" else p.get(f"cfg_{f['name']}", ""))
                         for f in meta["fields"]},
                      "skip_on_fail": p.get("skip_on_fail") == "on", "background": p.get("background") == "on"}
        bot.run_once = p.get("run_once") == "on"
        bot.success_tags = [t.strip() for t in p.get("success_tags", "").split(",") if t.strip()]
        bot.delay_minutes = int(p.get("delay_minutes") or 0)
        if p.get("condition_json", "").strip():
            try:
                bot.condition = json.loads(p["condition_json"])
            except ValueError:
                messages.error(request, "The advanced condition is not valid JSON — it was not changed.")
        else:
            bot.condition = _rules_from_post(p)
        bot.save()
        wf.touch()
        messages.success(request, f"Bot “{bot.name}” saved.")
        if bot.trigger == Bot.INLINE:
            return _back(wf)
        return redirect("flows:bots", wf.pk)
    steps = wf.ordered_steps()
    return render(request, "flows/bot_form.html", {
        "wf": wf, "bot": bot, "bot_type": bot_type, "meta": meta, "triggers": Bot.TRIGGERS,
        "steps": [s for s in steps if s.kind in (Step.RECIPIENT, Step.BOT)],
        "trigger": bot.trigger if bot else request.GET.get("trigger") or (meta or {}).get("default_trigger") or Bot.STEP_COMPLETE,
        "step_key": bot.step_key if bot else request.GET.get("step", ""),
        "config": bot.config if bot else {}, "fields": services.field_choices(wf), "ops": CONDITION_OPERATORS,
        "rules": ((bot.condition or {}).get("rules") if bot else None) or [{}],
        "logic": (bot.condition or {}).get("logic", "and") if bot else "and",
        "connections": Connection.objects.filter(workspace=wf.workspace),
        "connectors": __import__("flows.models", fromlist=["Connector"]).Connector.objects.filter(workspace=wf.workspace, active=True),
        "workflows": Workflow.objects.visible_to(request.membership).live().exclude(pk=wf.pk),
        "datasheets": DataSheet.objects.filter(workspace=wf.workspace),
        "all_steps": [s for s in steps if s.kind == Step.RECIPIENT], "groups": Group.objects.filter(workspace=wf.workspace),
        "nested": _is_nested(bot.condition if bot else {}, wf.documents.all()),
        "condition_json": json.dumps(bot.condition, indent=1) if bot and _is_nested(bot.condition, wf.documents.all()) else "",
    })


def _is_nested(condition, docs=()):
    """Needs the JSON editor: nested groups or internal keys (@step/@status/@document) the picker does not offer."""
    return any("rules" in r or (str(r.get("field", "")).startswith("@") and not condition_label(r["field"], docs))
               for r in (condition or {}).get("rules", []))


@login_required
@require_POST
def bot_action(request, pk, bot_id, action):
    wf = _editable(request, pk)
    bot = get_object_or_404(Bot, workflow=wf, pk=bot_id)
    if action == "delete":
        if bot.trigger == Bot.INLINE:
            wf.steps.filter(key=bot.step_key, kind=Step.BOT).delete()
        bot.delete()
    elif action == "toggle":
        bot.enabled = not bot.enabled
        bot.save(update_fields=["enabled"])
    wf.touch()
    return safe_next(request, "flows:bots", wf.pk)


@login_required
def preview_email(request, pk, step_id):
    wf = get_workflow(request, pk, edit=True)
    step = get_object_or_404(Step, workflow=wf, pk=step_id)
    ctx = {"user_name": step.recipient_name or "Recipient", "workflow_name": wf.name, "template_name": wf.name,
           "document_name": wf.name, "link": "#", "sender_name": engine.actor_label(request.user)}
    ctx["message"] = mailer.render(step.message_body, ctx)
    subject, body = mailer.get_template(wf.workspace, "invite")
    return render(request, "flows/partials/email_preview.html", {
        "subject": mailer.render(step.message_subject or subject, ctx), "body": mailer.render(body, ctx), "wf": wf})


