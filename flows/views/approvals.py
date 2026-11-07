"""Quick approvals: one-tap decisions on phones. A step qualifies when its recipient has a decision choice
(Approve / Reject / Decline / Accept …) and nothing else required is still empty — anything else opens normally."""
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .. import engine
from ..conditions import evaluate
from ..fieldtypes import is_input
from ..models import Packet, StepRun

DECISION = re.compile(r"approv|reject|declin|accept|deny|refus|authori[sz]e|return", re.I)
COMMENT = re.compile(r"comment|note|reason|remark", re.I)


def plan(run):
    """{decision: field, doc, comment: field|None, blocking: [labels], facts: [(label, value)]} or None."""
    from ..privacy import is_sensitive

    p = run.packet
    editable = engine.editable_field_ids(p, run.step_key)
    flat = p.flat_values()
    decision = comment = None
    blocking, facts = [], []
    for doc in p.documents:
        ids = editable.get(doc["key"], set())
        vals = (p.data or {}).get(doc["key"], {})
        for f in doc.get("fields", []):
            if not is_input(f.get("type")):
                continue
            if f["id"] in ids:
                if not evaluate(f.get("condition"), flat):
                    continue
                if decision is None and f.get("type") in ("radio", "dropdown") and any(DECISION.search(str(o)) for o in f.get("options") or []):
                    decision = {"field": f, "doc": doc["key"]}
                elif comment is None and f.get("type") in ("textarea", "text") and COMMENT.search(f.get("label") or f.get("name") or ""):
                    comment = {"field": f, "doc": doc["key"]}
                elif f.get("required") and vals.get(f["id"]) in (None, "", [], {}):
                    blocking.append(f.get("label") or f.get("name"))
            elif vals.get(f["id"]) not in (None, "", [], {}) and len(facts) < 6 and f.get("type") not in ("signature", "initials", "attachment"):
                v = vals[f["id"]]
                facts.append((f.get("label") or f.get("name"), "••••" if is_sensitive(f) else (", ".join(map(str, v)) if isinstance(v, list) else str(v))[:80]))
    if decision is None:
        return None
    if comment and comment["field"].get("required"):
        comment["required"] = True
    return {"decision": decision, "comment": comment, "blocking": blocking, "facts": facts}


def _tone(option):
    o = str(option).lower()
    return "good" if re.search(r"approv|accept|authori|yes", o) else "bad" if re.search(r"reject|declin|deny|refus|no\b", o) else "neutral"


@login_required
def approvals(request):
    from core.queries import my_runs

    runs = (my_runs(request.user, request.workspace).filter(status__in=[StepRun.PENDING, StepRun.OPENED], packet__status=Packet.IN_PROGRESS)
            .select_related("packet", "packet__workflow").order_by("due_at", "created_at").distinct()[:60])
    cards, others = [], 0
    for r in runs:
        pl = plan(r)
        if pl is None:
            others += 1
            continue
        opts = pl["decision"]["field"].get("options") or []
        cards.append({"run": r, "plan": pl, "options": [(o, _tone(o)) for o in opts],
                      "summary": ((r.packet.flags or {}).get("ai_summary") or {}).get("text", "")})
    return render(request, "flows/approvals.html", {"cards": cards, "others": others, "nav": "approvals"})


@login_required
@require_POST
def decide(request, token):
    run = get_object_or_404(StepRun.objects.select_related("packet", "packet__workspace"), token=token,
                            packet__workspace=request.workspace)
    if not run.is_open or run.packet.status != Packet.IN_PROGRESS:
        messages.info(request, "That step was already completed.")
        return redirect("flows:approvals")
    if not run.can_act(request.user):
        raise PermissionDenied("This step isn't yours to complete.")
    pl = plan(run)
    choice = request.POST.get("decision", "")
    if pl is None or pl["blocking"] or choice not in (pl["decision"]["field"].get("options") or []):
        messages.error(request, "This step needs more than a decision — open it to complete.")
        return redirect("flows:approvals")
    comment = request.POST.get("comment", "").strip()[:2000]
    if pl["comment"] and pl["comment"].get("required") and not comment:
        messages.error(request, f"Add a {pl['comment']['field'].get('label', 'comment').lower()} first.")
        return redirect("flows:approvals")
    values = {pl["decision"]["doc"]: {pl["decision"]["field"]["id"]: choice}}
    if pl["comment"] and comment:
        values.setdefault(pl["comment"]["doc"], {})[pl["comment"]["field"]["id"]] = comment
    if run.assignee_group_id and not run.claimed_by_id:
        run.claim(request.user)
    engine.complete_run(run, values, user=request.user, request=request)
    messages.success(request, f"{choice}: {run.packet.name}")
    return redirect("flows:approvals")


def decide_by_link(request, token, idx):
    """One-tap decision from the invitation email. GET only shows a confirmation, so mail scanners that open links
    can't decide anything; the POST completes the step. The step's identity check still applies."""
    from django.urls import reverse

    from .. import identity

    run = get_object_or_404(StepRun.objects.select_related("packet", "packet__workflow", "packet__workspace"), token=token)
    packet = run.packet
    if packet.status != Packet.IN_PROGRESS or not run.is_open:
        return render(request, "flows/fill_closed.html", {"run": run, "packet": packet, "workspace": packet.workspace})
    if run.assignee_group_id or (identity.rule(run) and not identity.passed(request, run)):
        return redirect("flows:fill", run.token)  # group steps need a sign-in; checks happen on the fill page
    pl = plan(run)
    opts = (pl or {}).get("decision", {}).get("field", {}).get("options") or []
    if not pl or pl["blocking"] or not 0 <= idx < len(opts):
        return redirect("flows:fill", run.token)
    choice = opts[idx]
    if request.method == "POST":
        comment = request.POST.get("comment", "").strip()[:2000]
        if pl["comment"] and pl["comment"].get("required") and not comment:
            return redirect("flows:fill", run.token)
        values = {pl["decision"]["doc"]: {pl["decision"]["field"]["id"]: choice}}
        if pl["comment"] and comment:
            values.setdefault(pl["comment"]["doc"], {})[pl["comment"]["field"]["id"]] = comment
        engine.mark_opened(run, request=request)
        engine.audit(packet.workspace, "step.one_tap", f"{run.step_name}: {choice} from the email", packet=packet, request=request)
        engine.complete_run(run, values, request=request, label=run.assignee_name or run.assignee_email)
        return render(request, "flows/fill_decided.html", {"packet": packet, "workspace": packet.workspace, "choice": choice})
    return render(request, "flows/fill_decide.html", {
        "run": run, "packet": packet, "workspace": packet.workspace, "choice": choice, "tone": _tone(choice), "plan": pl,
        "others": [(i, o) for i, o in enumerate(opts) if i != idx], "full": reverse("flows:fill", args=[run.token])})
