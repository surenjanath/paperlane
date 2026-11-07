"""Model test bench: run a model on six real Paperlane tasks with checkable answers and score it.
Results are kept per model in ws.settings["ai"]["benchmarks"] and shown on My workspace › AI & models."""
import threading
import time

from django.core.cache import cache
from django.db import close_old_connections
from django.utils import timezone

from . import ai_actions, ai_forms, ai_workflow

COMPLAINT = ("My husband passed away in June and I submitted the death claim with the certificate three weeks ago. "
             "Nobody has called me back and the claim still shows pending.")
REPORT = ("Claim POL-100003 for Riley Demo: the assessor approved a payout of TTD 15,000 on Oct 12. Payment is by direct "
          "deposit to the beneficiary. Finance must release funds within 5 business days; the beneficiary's bank letter is on file.")
WF_OUTLINE = ("STEPS (in order):\n- Step “Requester” → anyone; email subject: “Please complete your request”\n"
              "- Step “Manager approval” → group Managers; email subject: “You're invited to complete the documents”\n"
              "- Step “Finance” → group Finance\nDOCUMENT “Request” fields:\n- “Amount” (currency, required) filled by Requester\nBOTS: none")


def _t_form_build(ws):
    r = ai_forms.generate(ws, "A contact form with full name, email, phone number and a message.", [], "anyone")
    types = {f["type"] for f in r["fields"]}
    ok = {"email", "phone"} <= types and bool(types & {"textarea", "text"}) and len(r["fields"]) >= 4
    return ok, f"{len(r['fields'])} fields: {', '.join(sorted(types))}"


def _t_form_edit(ws):
    fields, _ = ai_forms.normalize([{"type": "text", "label": "Name"}, {"type": "phone", "label": "Phone"},
                                    {"type": "email", "label": "Email"}], [], "anyone")
    r = ai_forms.edit(ws, fields, "Make the Phone field required and add a Date of birth field after Name.", [], "anyone")
    labels = [f["label"].lower() for f in r["fields"]]
    phone = next((f for f in r["fields"] if f["label"].lower() == "phone"), {})
    birth = next((i for i, x in enumerate(labels) if "birth" in x), None)
    ok = bool(phone.get("required")) and birth == 1  # required, and the new field right after Name
    return ok, "; ".join(r["changes"]) or "no changes"


def _t_search(ws):
    f = ai_actions.search_filters(ws, "declined documents created this month", [], [])
    ok = f.get("status") == "declined" and "from" in f and not ({"quick", "q", "tag", "workflow"} & set(f))  # nothing invented
    return ok, str(f)


def _t_classify(ws):
    d = ai_actions.chat_json(ws, ai_actions.engine_for(ws), 'Classify the message. Reply with JSON {"category": "Billing"|"Claims"|"Other", '
                             '"urgency": "low"|"medium"|"high"}.', COMPLAINT, kind="classify")
    ok = d.get("category") == "Claims" and d.get("urgency") in ("medium", "high")
    return ok, str(d)


def _t_summary(ws):
    text = ai_actions.chat(ws, ai_actions.engine_for(ws), "Summarize for the next person in at most 40 words. Plain text.", REPORT,
                           json_mode=False, kind="summary").strip()
    words = len(text.split())
    ok = 3 <= words <= 60 and "15" in text
    return ok, f"{words} words: {text[:120]}"


def _t_workflow_edit(ws):
    d = ai_actions.chat_json(ws, ai_actions.engine_for(ws), ai_workflow.EDIT_SYSTEM,
                             f"{WF_OUTLINE}\n\nChange request: Change the Manager approval email subject to “Approval needed”.", kind="workflow_edit")
    edits = d.get("edits") or []
    ok = len(edits) == 1 and edits[0].get("op") == "update_step" and "manager" in str(edits[0].get("step", "")).lower() \
        and "approval needed" in str((edits[0].get("set") or {}).get("subject", "")).lower()
    return ok, f"{len(edits)} edit(s)"


TASKS = [("Design a form", _t_form_build), ("Edit a form", _t_form_edit), ("Search filters", _t_search),
         ("Classify a message", _t_classify), ("Summarize", _t_summary), ("Edit a workflow", _t_workflow_edit)]


def _key(ws):
    return f"ai-eval:{ws.pk}"


def run(ws, engine):
    """Run every task with ``engine`` (blocking). Returns the result dict and stores a summary on the workspace."""
    token = ai_actions._FORCE.set(engine)
    state = {"status": "running", "engine": engine, "total": len(TASKS), "results": [], "started": timezone.now().isoformat()}
    cache.set(_key(ws), state, 3600)
    try:
        for name, fn in TASKS:
            t0 = time.monotonic()
            try:
                ok, detail = fn(ws)
            except Exception as exc:  # noqa: BLE001 - a failing task is a result, not a crash
                ok, detail = False, f"{type(exc).__name__}: {str(exc)[:160]}"
            state["results"].append({"task": name, "ok": bool(ok), "ms": int((time.monotonic() - t0) * 1000), "detail": str(detail)[:200]})
            cache.set(_key(ws), state, 3600)
    finally:
        ai_actions._FORCE.reset(token)
    passed = sum(r["ok"] for r in state["results"])
    state.update(status="done", score=round(100 * passed / len(TASKS)), passed=passed,
                 avg_ms=int(sum(r["ms"] for r in state["results"]) / len(TASKS)))
    cache.set(_key(ws), state, 3600)
    ws.refresh_from_db()
    ai = dict((ws.settings or {}).get("ai") or {})
    marks = dict(ai.get("benchmarks") or {})
    marks[engine] = {"score": state["score"], "avg_ms": state["avg_ms"], "at": timezone.now().isoformat()[:16], "passed": passed}
    ai["benchmarks"] = marks
    ws.settings = {**(ws.settings or {}), "ai": ai}
    ws.save(update_fields=["settings"])
    return state


def start(ws, engine):
    """Run in a background thread; poll status(ws)."""
    if (cache.get(_key(ws)) or {}).get("status") == "running":
        return False
    cache.set(_key(ws), {"status": "running", "engine": engine, "total": len(TASKS), "results": []}, 3600)

    def work():
        try:
            run(ws, engine)
        finally:
            close_old_connections()
    threading.Thread(target=work, daemon=True).start()
    return True


def status(ws):
    return cache.get(_key(ws)) or {"status": "idle"}
