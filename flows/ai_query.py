"""Ask AI across all documents: the model turns a question into a structured query; Paperlane runs it with the user's
own permissions and computes the numbers itself (the model never sees document data or invents counts)."""
import datetime as dt
import json
import re

from django.db.models import Q
from django.utils import timezone

from . import ai_actions

OPS = ("eq", "neq", "gt", "gte", "lt", "lte", "contains", "empty", "not_empty")
SKIP = {"signature", "initials", "attachment", "heading", "description", "divider", "page_break", "html", "image", "youtube", "vimeo"}
DATA_WORDS = re.compile(r"\b(which|how many|list|show me|show all|find|count|total|sum|average|avg|number of|documents? (?:that|where|with)|"
                        r"claims?|requests?)\b", re.I)


def schema(membership, limit=25):
    """Workflows the user can see, with their fields and steps (names only)."""
    from .models import Workflow

    out = []
    for wf in Workflow.objects.visible_to(membership).filter(trashed_at__isnull=True, kind="workflow").order_by("-updated_at")[:limit]:
        snap = wf.runnable_snapshot()
        fields = []
        for d in snap.get("documents", []):
            for f in d.get("fields", []):
                if f.get("type") not in SKIP and (f.get("name") or f.get("label")):
                    fields.append(f"{f.get('name') or f.get('label')} ({f.get('type')})")
        steps = [s["name"] for s in snap.get("steps", []) if s.get("kind") == "recipient"]
        out.append({"workflow": wf.name, "fields": fields[:40], "steps": steps[:30]})
    return out


SYSTEM = """You turn a question about documents into ONE JSON query. Only use workflow, field and step names from the
SCHEMA. Reply with JSON only:
{"workflow": "<exact workflow name or null>", "status": "in_progress|completed|declined|cancelled|null",
 "step": "<current step name the document is waiting on, or null>", "created_from": "YYYY-MM-DD|null",
 "created_to": "YYYY-MM-DD|null", "fields": [{"field": "<field name>", "op": "eq|neq|gt|gte|lt|lte|contains|empty|not_empty", "value": "..."}],
 "overdue": true|false, "aggregate": "count|list|sum|avg", "of": "<number field for sum/avg or null>", "limit": 20}
Numbers are plain digits. "this month" means from the 1st of the current month. If the question isn't about documents,
reply {"aggregate": "none"}."""


def plan(membership, question, user=None):
    ws = membership.workspace
    engine = ai_actions.engine_for(ws)
    today = timezone.localdate()
    prompt = f"Today is {today:%A %Y-%m-%d}.\nSCHEMA: {json.dumps(schema(membership), ensure_ascii=False)}\n\nQuestion: {question[:500]}"
    q = ai_actions.chat_json(ws, engine, SYSTEM, prompt, kind="ask_all", user=user)
    return _clean(q)


def _clean(q):
    out = {"aggregate": q.get("aggregate") if q.get("aggregate") in ("count", "list", "sum", "avg", "none") else "list"}
    for k in ("workflow", "step", "of"):
        if isinstance(q.get(k), str) and q[k].strip() and q[k].strip().lower() != "null":
            out[k] = q[k].strip()[:200]
    if q.get("status") in ("in_progress", "completed", "declined", "cancelled"):
        out["status"] = q["status"]
    for k in ("created_from", "created_to"):
        try:
            out[k] = dt.date.fromisoformat(str(q.get(k, ""))[:10])
        except ValueError:
            pass
    out["fields"] = [{"field": str(f["field"])[:120], "op": f.get("op") if f.get("op") in OPS else "eq", "value": f.get("value")}
                     for f in (q.get("fields") or []) if isinstance(f, dict) and f.get("field")][:6]
    out["overdue"] = bool(q.get("overdue"))
    try:
        out["limit"] = max(1, min(50, int(q.get("limit") or 20)))
    except (TypeError, ValueError):
        out["limit"] = 20
    return out


def _num(v):
    try:
        return float(re.sub(r"[^\d.\-]", "", str(v)))
    except ValueError:
        return None


def _match(value, op, want):
    if op == "empty":
        return value in (None, "", [], {})
    if op == "not_empty":
        return value not in (None, "", [], {})
    if value in (None, "", [], {}):
        return False
    if op in ("gt", "gte", "lt", "lte"):
        a, b = _num(value), _num(want)
        if a is None or b is None:
            return False
        return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
    a, b = str(value).strip().lower(), str(want).strip().lower()
    if op == "contains":
        return b in a
    same = a == b or (_num(a) is not None and _num(a) == _num(b))
    return not same if op == "neq" else same


def _value(flat, name):
    if name in flat:
        return flat[name]
    low = name.lower()
    return next((v for k, v in flat.items() if str(k).lower() == low), None)


def run(membership, q, scan=2000):
    from .models import Packet, StepRun, Workflow

    qs = Packet.objects.visible_to(membership).live().filter(is_test=False).select_related("workflow")
    notes = []
    if q.get("workflow"):
        wf = Workflow.objects.visible_to(membership).filter(name__iexact=q["workflow"]).first() or \
             Workflow.objects.visible_to(membership).filter(name__icontains=q["workflow"]).first()
        if wf:
            qs = qs.filter(workflow=wf)
            q["workflow"] = wf.name
        else:
            notes.append(f"No workflow called “{q['workflow']}”.")
            q.pop("workflow")
    if q.get("status"):
        qs = qs.filter(status=q["status"])
    if q.get("created_from"):
        qs = qs.filter(created_at__date__gte=q["created_from"])
    if q.get("created_to"):
        qs = qs.filter(created_at__date__lte=q["created_to"])
    open_q = Q(runs__status__in=[StepRun.PENDING, StepRun.OPENED])
    if q.get("step"):
        qs = qs.filter(open_q, runs__step_name__icontains=q["step"]).distinct()
    if q.get("overdue"):
        qs = qs.filter(open_q, runs__due_at__lt=timezone.now()).distinct()
    rows, scanned = [], 0
    for p in qs.order_by("-created_at")[:scan]:
        scanned += 1
        flat = p.flat_values()
        if all(_match(_value(flat, f["field"]), f["op"], f["value"]) for f in q["fields"]):
            rows.append((p, flat))
    result = {"count": len(rows), "scanned": scanned, "truncated": scanned >= scan, "notes": notes}
    if q["aggregate"] in ("sum", "avg") and q.get("of"):
        nums = [n for n in (_num(_value(flat, q["of"])) for _, flat in rows) if n is not None]
        result["value"] = round(sum(nums) / len(nums), 2) if q["aggregate"] == "avg" and nums else round(sum(nums), 2)
        result["value_of"] = len(nums)
    shown = [f["field"] for f in q["fields"]] + ([q["of"]] if q.get("of") else [])
    from .privacy import mask, sensitive_names

    def shown_value(p, flat, k):
        v = _value(flat, k)
        return mask(v) if k in sensitive_names(p.documents) or str(k).lower() in {x.lower() for x in sensitive_names(p.documents)} else v

    result["rows"] = [{"id": p.pk, "name": p.name, "url": p.get_absolute_url(), "status": p.get_status_display(),
                       "step": (p.current_step() or {}).get("name", "") if p.status == "in_progress" else "",
                       "values": {k: str(shown_value(p, flat, k))[:60] for k in dict.fromkeys(shown) if _value(flat, k) not in (None, "")}}
                      for p, flat in rows[:q["limit"]]]
    return result


def describe(q):
    parts = []
    if q.get("workflow"):
        parts.append(f"workflow “{q['workflow']}”")
    if q.get("status"):
        parts.append(q["status"].replace("_", " "))
    if q.get("step"):
        parts.append(f"waiting on “{q['step']}”")
    if q.get("overdue"):
        parts.append("overdue")
    sym = {"eq": "=", "neq": "≠", "gt": ">", "gte": "≥", "lt": "<", "lte": "≤", "contains": "contains", "empty": "is empty", "not_empty": "is filled"}
    parts += [f"{f['field']} {sym[f['op']]}{'' if f['op'] in ('empty', 'not_empty') else ' ' + str(f['value'])}" for f in q["fields"]]
    if q.get("created_from") or q.get("created_to"):
        parts.append(f"created {q.get('created_from') or '…'} → {q.get('created_to') or 'today'}")
    return parts


def answer(membership, question, user=None):
    q = plan(membership, question, user)
    if q["aggregate"] == "none":
        return None
    r = run(membership, q)
    n = r["count"]
    if q["aggregate"] in ("sum", "avg") and "value" in r:
        text = (f"The {'average' if q['aggregate'] == 'avg' else 'total'} **{q['of']}** is **{r['value']:,}** "
                f"across {r['value_of']} matching document{'s' if r['value_of'] != 1 else ''}.")
    else:
        text = (f"**{n}** documents match." if n != 1 else "**1** document matches.") if n else "No documents match."
    if r["truncated"]:
        text += f" (Only the newest {r['scanned']} documents were checked.)"
    for note in r["notes"]:
        text += f" {note}"
    return {"answer": text, "query": {"parts": describe(q), "count": n, "rows": r["rows"],
                                      "value": r.get("value"), "aggregate": q["aggregate"], "of": q.get("of")}}
