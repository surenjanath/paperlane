"""“Describe your workflow”: turn a plain-language description into a workflow blueprint.

Two engines:
* Language models (see flows/ai_providers.py): Claude, local Ollama models and OpenAI-compatible servers. They get
  the blueprint rules plus a worked example; replies are cleaned up (normalize), dry-run, and sent back once for
  fixing if they don't build. If a model fails, the built-in planner is used and the page says so.
* The built-in planner — rule-based, no network. Understands sentences such as
  “Employee submits a request with item, quantity, amount and reason. Manager approves. If amount is over 5,000
  finance approves. Procurement enters the PO number. Email the requester when done.”

Every plan is dry-run (built inside a rolled-back transaction) so the preview shows exactly what will be created.
"""
import json
import os
import re

from django.db import transaction

from . import ai_providers
from . import blueprint as bp

DEFAULT_MODEL = "claude-opus-5-5"

EXAMPLES = [
    ("Purchase approval",
     "Purchase approval: an employee submits a request with item, quantity, amount, needed by date and reason. "
     "Their manager approves. If amount is over 5,000, finance approves. Then procurement enters the PO number. "
     "Email the requester when done."),
    ("Leave request",
     "Leave request: employee fills in leave type, start date, end date and notes. Supervisor approves. "
     "HR records the leave balance. Notify the employee when done."),
    ("Vendor onboarding",
     "Vendor onboarding: vendor submits company name, contact email, tax ID and bank letter upload. "
     "Procurement reviews the documents. If amount is over 50,000, legal approves. Finance enters the vendor ID."),
    ("Insurance claim",
     "Claim intake: claimant submits policy number, incident date, claim amount, description and photos. "
     "Assessor reviews the claim. If claim amount is over 10,000, senior assessor approves. Finance records the payment reference."),
]


# ---- shared -------------------------------------------------------------------------------------
class _Rollback(Exception):
    pass


def dry_run(workspace, user, blueprint):
    """Build the blueprint inside a transaction that is rolled back. Returns a preview dict (or raises BlueprintError)."""
    preview = {}
    try:
        with transaction.atomic():
            wf, problems = bp.build_workflow(workspace, user, blueprint, publish=False)
            exported = bp.export_workflow(wf)
            names = {s.key: s.name for s in wf.steps.all()}
            fills = {}
            for d in exported["documents"]:
                for f in d["fields"]:
                    if f.get("type") in ("heading", "divider", "page_break", "description"):
                        continue
                    for k in f["filled_by"] if isinstance(f["filled_by"], list) else []:
                        fills.setdefault(k, []).append({"label": f.get("label"), "type": f.get("type"),
                                                        "required": bool(f.get("required")), "options": f.get("options")})
            preview = {"name": wf.name, "problems": problems, "steps": _describe(exported["steps"], names, fills),
                       "documents": [{"name": d["name"], "count": len(d["fields"])} for d in exported["documents"]],
                       "bots": [{"name": b["name"], "when": _rules_text(b.get("when"), names)} for b in exported["bots"]],
                       "hints": _hints(exported["steps"], fills),
                       "field_count": sum(len(d["fields"]) for d in exported["documents"]),
                       "step_count": wf.steps.filter(kind="recipient").count(),
                       "warnings": [f["message"] for f in __import__("flows.checks", fromlist=["run"]).run(wf)
                                    if f["level"] == "warning"]}
            raise _Rollback
    except _Rollback:
        pass
    return preview


def _hints(steps, fills):
    out = []
    for s in steps:
        if s["type"] == "step" and not fills.get(s["key"]):
            out.append(f"“{s['name']}” has nothing to fill, so it works as a view-and-confirm step. "
                       "Remove it in the builder if a bot email is enough.")
        for ln in s.get("lanes") or []:
            out += _hints(ln.get("steps") or [], fills)
    return out


OP_TEXT = {"eq": "is", "neq": "is not", "contains": "contains", "not_contains": "doesn't contain", "empty": "is empty",
           "not_empty": "is filled", "gt": ">", "lt": "<", "gte": "≥", "lte": "≤", "checked": "is checked",
           "not_checked": "is not checked", "regex": "matches", "after": "is after", "until": "is until"}


def _rules_text(cond, names):
    if not cond or not cond.get("rules"):
        return ""
    parts = []
    for r in cond["rules"]:
        if "rules" in r:
            parts.append(f"({_rules_text(r, names)})")
            continue
        value = names.get(r.get("value"), r.get("value", ""))
        op = OP_TEXT.get(r.get("op"), r.get("op"))
        parts.append(f"{r.get('field')} {op}" + (f" {value}" if r.get("op") not in ("empty", "not_empty", "checked", "not_checked") else ""))
    return f" {cond.get('logic', 'and')} ".join(parts)


def _target_text(t, names):
    return {"next": "continue after the branches", "merge": "continue after the branches", "end": "complete the workflow",
            "lane": "run this lane's steps"}.get(t) or f"go to {names.get(t, t)}"


def _describe(steps, names, fills):
    out = []
    for s in steps:
        d = {"name": s["name"], "type": s["type"], "fills": fills.get(s["key"], []),
             "when": _rules_text(s.get("condition"), names),
             "jumps": [{"to": names.get(j["to"], j["to"]), "when": _rules_text(j.get("when"), names)} for j in s.get("jumps") or []]}
        r = s.get("recipient") or {}
        d["to"] = {"anyone": "Anyone with the link", "current_user": "Whoever starts it"}.get(r.get("type")) or \
            (f"Group: {r['group']}" if r.get("group") else r.get("email") or "")
        if s["type"] == "branch":
            d["lanes"] = [{"label": ln.get("label"), "when": _rules_text(ln.get("when"), names) or "always",
                           "steps": _describe(ln.get("steps") or [], names, fills),
                           "exit": _target_text(ln.get("then") if ln.get("steps") else ln.get("target"), names)}
                          for ln in s.get("lanes") or []]
        out.append(d)
    return out


def plan(text, workspace=None, user=None, engine=None):
    """Returns {"blueprint", "engine", "engine_label", "notes"}. Model engines fall back to the planner on failure."""
    from . import ai_actions

    text = (text or "").strip()
    if not text:
        raise bp.BlueprintError("Describe the workflow first.")
    engine = engine or ai_providers.default_engine()
    notes = []
    if engine != ai_providers.PLANNER:
        if not ai_providers.is_available(engine):
            notes.append(f"{engine} isn't available right now (is the server running?); the built-in planner was used instead.")
        else:
            try:
                blueprint, more = plan_with_model(engine, text, workspace, user)
                return {"blueprint": blueprint, "engine": engine, "engine_label": ai_providers.label(engine), "notes": more}
            except (ai_providers.EngineError, ai_actions.AIError, ValueError, bp.BlueprintError) as exc:
                notes.append(f"{ai_providers.label(engine)} couldn't produce a valid workflow ({str(exc)[:200]}); "
                             "the built-in planner was used instead.")
    blueprint, more = plan_with_rules(text)
    return {"blueprint": blueprint, "engine": ai_providers.PLANNER, "engine_label": "Built-in planner", "notes": notes + more}


# ---- language models ---------------------------------------------------------------------------
SYSTEM = """You design document workflows for Paperlane. Reply with ONE JSON object only: a Paperlane workflow blueprint.
No prose, no markdown, no comments.

Rules:
- Top-level keys: name, description, documents, steps, bots.
- Use one document (key "main") unless the process clearly needs more. Every field has: id (unique snake_case),
  type (from FIELD TYPES), label, name (same as label), filled_by (list of step keys), optional required/options.
- steps is an ordered list. A person step: {"key", "name", "recipient"}. The first step starts the request and has
  recipient {"type": "anyone"}. Others use {"type": "group", "group": "<Role plural>"} or {"type": "email", "email": "..."}.
- Approvers fill a required radio field "<Step name> decision" with options ["Approve", "Reject"]. After an approval add
  a branch step: {"key": "<k>_outcome", "type": "branch", "name": "...", "lanes": [{"id": "rejected", "label": "Rejected",
  "target": "end", "when": {"rules": [{"field": "<Step name> decision", "op": "eq", "value": "Reject"}]}}]}.
- A step that only happens sometimes gets "condition": {"rules": [{"field": "<field name>", "op": "gt", "value": "5000"}]}.
  Numbers are plain digits. Condition fields must be field names that exist.
- Reviews that can send work back use "jumps": [{"to": "<earlier step key>", "when": {"rules": [...]}}].
- Bots (optional): rename_packet {"pattern": "Text {field:Name}"}, email_on_complete {"to": "{field:Email}", "subject", "body"},
  add_tags {"tags": "#Tag"}. A bot has type, name, trigger (step_complete | flow_complete | step_start), optional step, config.
"""


def _compact_guide():
    g = bp.guide()
    keep = ("rename_packet", "add_tags", "email_on_complete", "email_custom", "webhook", "export_to_sheet", "lock_packet")
    return {"field_types": [t for t in g["field_types"] if t not in ("heading", "description", "divider", "page_break", "html",
                                                                     "youtube", "vimeo", "image", "matrix", "group")],
            "condition_operators": g["condition_operators"], "bot_triggers": g["bot_triggers"],
            "bot_types": {k: v for k, v in g["bot_types"].items() if k in keep}, "recipient_types": g["recipient_types"]}


def _example():
    example, _ = plan_with_rules(EXAMPLES[0][1])
    return example


def plan_with_model(engine, text, workspace=None, user=None):
    """Ask a language model for a blueprint; clean it up, check it, and give the model one chance to fix errors."""
    from . import ai_actions

    system = (SYSTEM + "\nFIELD TYPES, OPERATORS, BOTS: " + json.dumps(_compact_guide())
              + "\n\nEXAMPLE for the request “" + EXAMPLES[0][1] + "”:\n" + json.dumps(_example()))
    messages = [{"role": "user", "content": f"Build this workflow as a Paperlane blueprint JSON object:\n\n{text}"}]
    last_error = ""
    for attempt in range(2):
        if workspace is not None:
            reply = ai_actions.chat(workspace, engine, system, messages, kind="describe", user=user)
        else:
            reply = ai_providers.chat(engine, system, messages)
        try:
            blueprint, notes = normalize(extract_json(reply))
            if workspace is not None and user is not None:
                preview = dry_run(workspace, user, blueprint)  # raises BlueprintError on structural problems
                if attempt == 0 and preview.get("problems"):
                    raise bp.BlueprintError("; ".join(preview["problems"][:4]))
            return blueprint, notes
        except (ValueError, bp.BlueprintError) as exc:
            last_error = str(exc)
            if workspace is not None:
                ai_actions.note_failure(workspace, last_error, reply)
            messages += [{"role": "assistant", "content": reply[:12000]},
                         {"role": "user", "content": f"That blueprint has a problem: {last_error}\n"
                                                     "Return the complete corrected JSON object only."}]
    raise bp.BlueprintError(last_error)


def plan_with_claude(text):
    """Kept for callers that want Claude explicitly."""
    return plan_with_model(f"claude:{os.environ.get('PAPERLANE_AI_MODEL', DEFAULT_MODEL)}", text)[0]


def _scan_json(raw, end=None):
    """Open braces up to ``end``, ignoring characters inside strings."""
    stack, in_str, esc = [], False, False
    for ch in raw[:end]:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack and ((ch == "}" and stack[-1] == "{") or (ch == "]" and stack[-1] == "[")):
            stack.pop()
    return stack, in_str


_CLOSER = {"{": "}", "[": "]"}


def _repair_brackets(raw):
    """Rebuild the bracket structure a model got wrong: insert closers it forgot, drop strays, cut trailing text.

    Strings are copied untouched. When a closer does not match the innermost open bracket but matches one further
    out, the brackets in between are closed first (the model forgot them); a closer that matches nothing is dropped.
    Trailing commas before a closer are removed, and the text stops once the top-level object is complete."""
    out, stack, in_str, esc = [], [], False, False
    for ch in raw:
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
        elif ch in "{[":
            stack.append(ch)
            out.append(ch)
        elif ch in "}]":
            opener = "{" if ch == "}" else "["
            if opener not in stack:
                continue  # stray closer
            while stack[-1] != opener:
                _close(out, stack.pop())
            _close(out, stack.pop())
            if not stack:
                return "".join(out)
        else:
            out.append(ch)
    if in_str:
        out.append('"')
    while stack:
        _close(out, stack.pop())
    return "".join(out)


def _close(out, opener):
    while out and (out[-1].isspace() or out[-1] == ","):
        out.pop()
    out.append(_CLOSER[opener])


def _loads_loose(raw):
    """json.loads, then a structural repair for the brackets and commas a model got wrong."""
    raw = (raw or "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        try:
            return json.loads(_repair_brackets(raw))
        except json.JSONDecodeError:
            raise ValueError(f"the reply was not valid JSON ({exc.msg})") from exc


_THINK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.S | re.I)


def extract_json(reply):
    reply = _THINK.sub("", reply or "")  # reasoning models may prepend their thinking
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", reply, re.S)
    start = reply.find("{")
    # from the first brace to the end: the repair stops at the end of the object, and closes one that was cut off
    raw = m.group(1) if m else (reply[start:] if start >= 0 else "")
    if not raw.strip():
        raise ValueError("the reply contained no JSON object")
    try:
        obj = _loads_loose(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"the reply was not valid JSON ({exc.msg})") from exc
    if isinstance(obj, dict) and "blueprint" in obj and isinstance(obj["blueprint"], dict):
        obj = obj["blueprint"]
    if not isinstance(obj, dict):
        raise ValueError("the reply was not a JSON object")
    return obj


TYPE_SYNONYMS = {"string": "text", "str": "text", "short_text": "text", "input": "text", "varchar": "text",
                 "long_text": "textarea", "paragraph": "textarea", "multiline": "textarea", "text_area": "textarea",
                 "integer": "number", "int": "number", "float": "number", "decimal": "number", "numeric": "number",
                 "money": "currency", "amount": "currency", "price": "currency", "file": "attachment", "upload": "attachment",
                 "file_upload": "attachment", "boolean": "checkbox", "bool": "checkbox", "yes_no": "radio", "select": "dropdown",
                 "choice": "radio", "radio_group": "radio", "datetime": "date", "date_time": "date", "telephone": "phone",
                 "tel": "phone", "e-mail": "email", "esign": "signature", "e_signature": "signature", "name": "text"}
OP_SYNONYMS = {">": "gt", "<": "lt", ">=": "gte", "<=": "lte", "=": "eq", "==": "eq", "!=": "neq", "equals": "eq",
               "is": "eq", "not_equals": "neq", "greater_than": "gt", "less_than": "lt", "greater": "gt", "less": "lt",
               "greater_or_equal": "gte", "less_or_equal": "lte", "over": "gt", "under": "lt", "is_empty": "empty",
               "is_not_empty": "not_empty", "filled": "not_empty"}


def normalize(spec):
    """Fix the slips language models commonly make (missing ids/keys, synonyms, step names instead of keys…)."""
    import copy

    spec = copy.deepcopy(spec)
    notes = []
    spec["name"] = str(spec.get("name") or spec.get("title") or "New workflow").strip()[:200]
    docs = spec.get("documents") or ([{"key": "main", "name": f"{spec['name']} form", "fields": spec.pop("fields")}]
                                     if isinstance(spec.get("fields"), list) else [])
    spec["documents"] = docs

    # steps: keys, names, nested lanes
    seen, by_name = set(), {}

    def fix_steps(steps):
        out = []
        for s in steps or []:
            if not isinstance(s, dict):
                continue
            s["name"] = str(s.get("name") or s.get("title") or s.get("key") or "Step")[:120]
            key = _slug(str(s.get("key") or s.get("id") or s["name"]))
            while key in seen:
                key += "_2"
            seen.add(key)
            by_name[s["name"].lower()] = key
            if s.get("key") and s["key"] != key:
                by_name[str(s["key"]).lower()] = key
            s["key"] = key
            t = str(s.get("type") or "step").lower()
            s["type"] = "branch" if t in ("branch", "condition", "decision", "gateway", "split") else "bot" if t == "bot" else "step"
            if s["type"] == "step" and not isinstance(s.get("recipient"), dict):
                r = s.get("recipient") or s.get("assignee") or s.get("role")
                if isinstance(r, str) and "@" in r:
                    s["recipient"] = {"type": "email", "email": r}
                elif isinstance(r, str) and r.lower() in ("anyone", "requester", "public"):
                    s["recipient"] = {"type": "anyone"}
                else:
                    s["recipient"] = {"type": "group", "group": _group_name(str(r or s["name"]).replace(" approval", "").replace(" review", ""))}
            if s["type"] == "branch":
                for i, ln in enumerate(s.get("lanes") or s.get("branches") or []):
                    ln.setdefault("id", f"lane{i + 1}")
                    ln["steps"] = fix_steps(ln.get("steps"))
                s["lanes"] = s.get("lanes") or s.pop("branches", [])
            out.append(s)
        return out

    spec["steps"] = fix_steps(spec.get("steps"))
    if spec["steps"] and spec["steps"][0]["type"] == "step":
        spec["steps"][0]["recipient"] = {"type": "anyone"}

    def key_of(ref):
        r = str(ref or "")
        return r if r in seen else by_name.get(r.lower(), r)

    field_names = set()
    for d in docs:
        d.setdefault("key", _slug(d.get("name") or "main"))
        d.setdefault("name", f"{spec['name']} form")
        ids = set()
        for f in d.get("fields") or []:
            f["label"] = str(f.get("label") or f.get("name") or f.get("id") or "Field")[:120]
            f.setdefault("name", f["label"])
            fid = _slug(str(f.get("id") or f["label"]))
            while fid in ids:
                fid += "_2"
            ids.add(fid)
            f["id"] = fid
            t = str(f.get("type") or "text").lower().replace(" ", "_")
            t = TYPE_SYNONYMS.get(t, t)
            if t not in bp.FIELD_TYPES:
                notes.append(f"Field “{f['label']}” had an unknown type “{f.get('type')}”, so it's a text field.")
                t = "text"
            f["type"] = t
            if t in ("radio", "dropdown", "checkbox") and isinstance(f.get("options"), str):
                f["options"] = [o.strip() for o in f["options"].split(",") if o.strip()]
            if t in ("radio", "dropdown") and not f.get("options"):
                f["options"] = ["Yes", "No"]
            by = f.get("filled_by", f.get("role", "anyone"))
            if by not in (None, "", "anyone"):
                by = by if isinstance(by, list) else [by]
                fixed = [key_of(b) for b in by]
                f["filled_by"] = [b for b in fixed if b in seen] or "anyone"
            else:
                f["filled_by"] = "anyone"
            f.pop("role", None)
            field_names.add(f["name"])

    def fix_cond(c):
        if not c:
            return c
        if isinstance(c, list):
            c = {"logic": "and", "rules": c}
        for r in c.get("rules") or []:
            if "rules" in r:
                fix_cond(r)
                continue
            op = str(r.get("op") or r.get("operator") or "eq").lower()
            r["op"] = OP_SYNONYMS.get(op, op)
            r.pop("operator", None)
            v = r.get("value")
            if isinstance(v, str) and re.fullmatch(r"[$£€]?\d[\d,]*(\.\d+)?", v.strip()):
                r["value"] = re.sub(r"[^\d.]", "", v)
            elif isinstance(v, (int, float)):
                r["value"] = str(int(v)) if float(v) == int(v) else str(v)
        return c

    def walk(steps):
        for s in steps:
            if s.get("condition"):
                s["condition"] = fix_cond(s["condition"])
            for j in s.get("jumps") or []:
                j["to"] = key_of(j.get("to"))
                j["when"] = fix_cond(j.get("when") or j.get("condition"))
            s["jumps"] = [j for j in s.get("jumps") or [] if j.get("to") in seen]
            for ln in s.get("lanes") or []:
                ln["when"] = fix_cond(ln.get("when") or ln.get("condition"))
                for k in ("target", "then"):
                    if ln.get(k) and ln[k] not in ("lane", "next", "merge", "end"):
                        ln[k] = key_of(ln[k]) if key_of(ln[k]) in seen else ("end" if "end" in str(ln[k]).lower() or "complete" in str(ln[k]).lower() else "next")
                walk(ln.get("steps") or [])
    walk(spec["steps"])

    bots = []
    for b in spec.get("bots") or []:
        if not isinstance(b, dict) or b.get("type") not in bp.CATALOG:
            notes.append(f"Skipped a bot of unknown type “{(b or {}).get('type')}”.")
            continue
        b["name"] = str(b.get("name") or bp.CATALOG[b["type"]]["name"])[:160]
        if b.get("trigger") not in bp.TRIGGERS:
            b["trigger"] = "flow_complete" if "complete" in str(b.get("trigger", "")) and not b.get("step") else "step_complete"
        if b.get("step"):
            b["step"] = key_of(b["step"])
            if b["step"] not in seen:
                b.pop("step")
        if b.get("when"):
            b["when"] = fix_cond(b["when"])
        bots.append(b)
    spec["bots"] = bots

    # {field:X} tokens must name real fields; an email bot's recipient gets an Email field if it's missing
    lower = {n.lower(): n for n in field_names}
    first = spec["steps"][0]["key"] if spec["steps"] else None
    for b in bots:
        for k, v in list((b.get("config") or {}).items()):
            if not isinstance(v, str):
                continue
            for token in re.findall(r"\{field:([^}]+)\}", v):
                if token in field_names:
                    continue
                if token.lower() in lower:
                    v = v.replace(f"{{field:{token}}}", f"{{field:{lower[token.lower()]}}}")
                elif k in ("to", "recipient") and docs and first:
                    email = next((n for n in field_names if "email" in n.lower()), None)
                    if email is None:
                        email = "Email"
                        docs[0].setdefault("fields", []).append({"id": "email", "type": "email", "label": "Email", "name": "Email",
                                                                 "required": True, "filled_by": [first]})
                        field_names.add(email)
                        lower[email.lower()] = email
                        notes.append(f"Added an “Email” field to the first step for the bot “{b.get('name')}”.")
                    v = v.replace(f"{{field:{token}}}", f"{{field:{email}}}")
                else:
                    notes.append(f"Bot “{b.get('name')}” mentions a field “{token}” that doesn't exist — check it in the builder.")
            b["config"][k] = v
    return spec, notes


# ---- built-in planner --------------------------------------------------------------------------
OPS = [  # phrase → operator (longest phrases first)
    ("greater than or equal to", "gte"), ("less than or equal to", "lte"), ("more than", "gt"), ("greater than", "gt"),
    ("less than", "lt"), ("at least", "gte"), ("at most", "lte"), ("exceeds", "gt"), ("over", "gt"), ("above", "gt"),
    ("under", "lt"), ("below", "lt"), (">=", "gte"), ("<=", "lte"), (">", "gt"), ("<", "lt"), ("equals", "eq"),
    ("is not", "neq"), ("is", "eq"), ("=", "eq"),
]
VERBS = {
    "start": ["submits", "submit", "fills in", "fills out", "fills", "requests", "request", "applies", "apply", "completes",
              "starts", "raises", "creates", "opens", "files"],
    "approve": ["approves", "approve", "must approve", "signs off", "sign off", "authorizes", "authorises", "authorize", "approval"],
    "review": ["reviews", "review", "checks", "check", "verifies", "verify", "assesses", "assess", "validates", "inspects"],
    "sign": ["signs", "sign", "countersigns", "e-signs"],
    "task": ["enters", "enter", "records", "record", "places", "place", "issues", "issue", "processes", "process", "pays",
             "adds", "add", "provides", "provide", "uploads", "upload", "prepares", "prepare", "confirms", "confirm",
             "sets up", "set up", "creates", "updates", "update", "assigns", "assign", "schedules", "schedule", "sends", "books"],
}
STARTER_WORDS = {"employee", "requester", "customer", "client", "applicant", "claimant", "vendor", "supplier", "user",
                 "staff", "member", "candidate", "student", "patient", "tenant", "partner", "agent", "someone", "anyone"}
NOTIFY = re.compile(r"\b(email|notify|inform|let .{1,20} know|send (?:an? )?(?:email|confirmation|notification))\b", re.I)
FIELD_LIST = re.compile(r"\b(?:with|including|includes|collecting|collects|asking for|provides?|enters?|records?|adds?|"
                        r"uploads?|attach(?:es)?|fills in|fills out|lists?|gives?)\s+(?:the |their |his |her |a |an |its )?(.+)$", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
STOP = {"the", "a", "an", "their", "his", "her", "its", "of", "details", "info", "information"}


def _title(s):
    s = re.sub(r"\s+", " ", s).strip(" .,:;")
    return s[:1].upper() + s[1:] if s else s


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:40] or "field"


def field_type(label):
    l = label.lower()
    if re.search(r"\b(reference|ref|id|code|number|no|#)$", l) and not re.search(r"\b(phone|mobile)\b", l) \
            and not l.startswith("number of"):
        return "text"
    rules = [
        (("email", "e-mail"), "email"), (("phone", "mobile", "telephone", "cell"), "phone"),
        (("signature", "sign here"), "signature"),
        (("date", "deadline", "dob", "birthday", "needed by", "due"), "date"),
        (("amount", "price", "cost", "total", "salary", "budget", "fee", "payment", "value", "premium", "spend"), "currency"),
        (("quantity", "qty", "number of", "count", "days", "hours", "age", "balance"), "number"),
        (("upload", "attachment", "file", "photo", "picture", "scan", "letter", "receipt", "invoice copy", "document", "pdf", "cv", "resume"), "attachment"),
        (("description", "reason", "notes", "comment", "details", "explanation", "justification", "summary", "address"), "textarea"),
        (("agree", "consent", "confirm that", "accept"), "checkbox"),
    ]
    for words, t in rules:
        if any(w in l for w in words):
            if t == "number" and l.endswith(" number") and not l.startswith("number"):  # "PO number", "policy number" are ids
                return "text"
            return t
    return "text"


def _split_fields(phrase):
    phrase = re.split(r"\b(?:when|if|then|before|after|so that|because|and then)\b|[.;:]", phrase, maxsplit=1, flags=re.I)[0]
    parts = re.split(r",\s*(?:and\s+|or\s+)?|\s+and\s+|\s+&\s+|\s+or\s+", phrase)
    out = []
    for p in parts:
        words = [w for w in re.sub(r"[^\w\s/-]", " ", p).split() if w.lower() not in STOP]
        label = " ".join(words)[:48]
        if 1 < len(label) and not label.lower() in ("it", "them", "this", "that", "request", "form", "order", "documents"):
            out.append(_title(label))
    return out


def _actor_name(raw):
    words = [w for w in re.sub(r"[^\w\s-]", " ", raw).split() if w.lower() not in {"the", "a", "an", "their", "his", "her",
                                                                                   "its", "then", "and", "finally", "next",
                                                                                   "after", "that", "which", "who"}]
    return " ".join(words[-3:])


def _group_name(actor):
    a = actor.strip()
    if not a:
        return "Approvers"
    if a.isupper():
        return a
    t = " ".join(w if w.isupper() else w.capitalize() for w in a.split())
    if re.search(r"(er|or|ist|ant|ian|ee|ive)$", t) and not t.endswith("s"):
        t += "s"
    return t


def _parse_condition(sentence):
    """('field phrase', op, value, sentence without the condition) or None."""
    m = re.search(r"\b(?:if|when|where|only if|only when|for|in case)\s+(?:the\s+)?([a-z][\w\s-]{0,40}?)\s+(?:is\s+|are\s+)?"
                  r"(" + "|".join(re.escape(p) for p, _ in OPS) + r")\s+(?:than\s+)?\$?([\d][\d,.]*\s*[km]?|[\w-]+)", sentence, re.I)
    if not m:
        return None
    op = dict(OPS)[m.group(2).lower()]
    value = m.group(3).strip().lower().replace(",", "")
    mult = 1000 if value.endswith("k") else 1_000_000 if value.endswith("m") and value[:-1].replace(".", "").isdigit() else 1
    if mult > 1:
        value = value[:-1].strip()
    try:
        num = float(value) * mult
        value = str(int(num)) if num == int(num) else str(num)
    except ValueError:
        if op not in ("eq", "neq"):
            return None
    rest = (sentence[:m.start()] + " " + sentence[m.end():]).strip(" ,")
    return m.group(1).strip(), op, value, rest


def _find_verb(sentence):
    low = " " + sentence.lower() + " "
    best = None
    for cat, words in VERBS.items():
        for w in words:
            i = low.find(f" {w} ")
            if i >= 0 and (best is None or i < best[1]):
                best = (cat, i, w)
    return best


def plan_with_rules(text):
    notes = []
    raw = text.strip()
    name = ""
    m = re.match(r"^\s*([^:\n]{3,60}):\s*(.+)$", raw, re.S)
    if m:
        name, raw = _title(m.group(1)), m.group(2)
    if not name:
        m = re.search(r"\b((?:[a-z]+\s){0,2}(?:approval|request|onboarding|intake|claim|application|review|process|change|order|registration))\b", raw, re.I)
        name = _title(m.group(1)) if m else "New workflow"

    sentences = [s.strip() for s in re.split(r"(?<=[.!?;\n])\s+|\s*(?:→|->)\s*|\bthen\b,?|\bafter that\b,?|\bnext\b,?|\bfinally\b,?", raw, flags=re.I)
                 if s and s.strip(" .,")]
    steps, fields, bots = [], [], []
    notify_requester = False

    for sentence in sentences:
        s = sentence.strip().rstrip(".")
        if NOTIFY.search(s) and re.search(r"\b(done|complete|completed|finished|end|approved|at the end|once)\b", s, re.I) \
                or (NOTIFY.search(s) and not _find_verb(s)):
            notify_requester = True
            continue
        deadline = None
        dm = re.search(r"\bwithin\s+(\d+(?:\.\d+)?)\s+(business days?|working days?|days?|hours?)\b", s, re.I)
        if dm:
            unit = dm.group(2).lower()
            deadline = {"amount": float(dm.group(1)), "unit": "business_days" if unit.startswith(("business", "working"))
                        else "hours" if unit.startswith("hour") else "days", "escalate": "notify"}
            s = (s[:dm.start()] + s[dm.end():]).strip()
        cond = _parse_condition(s)
        if cond:
            s = cond[3]
        verb = _find_verb(s)
        if verb is None:
            if cond and steps:  # "if amount is over 5000" alone: attach to the next step
                steps.append({"pending_condition": cond})
            else:
                notes.append(f"Skipped: “{sentence.strip()}” (no step found).")
            continue
        cat, idx, word = verb
        before, after = s[:idx].strip(), s[idx + len(word) + 1:].strip()
        actor = _actor_name(before)
        if not actor:  # "Approval by finance", "approve: manager"
            mm = re.search(r"\bby\s+(?:the\s+|a\s+|an\s+)?([\w\s-]{2,30})", after, re.I)
            actor = _actor_name(mm.group(1)) if mm else ""
        if cat == "approve" and word == "approval" and before:
            actor = _actor_name(before)
        email = EMAIL.search(s)
        first = not [x for x in steps if "key" in x]
        if cat == "start" and not first:
            cat = "task"
        if first and cat not in ("start",) and (actor.lower() in STARTER_WORDS or not actor):
            cat = "start"
        key = _slug(actor or cat) + ("" if not any(x.get("key") == _slug(actor or cat) for x in steps) else f"_{len(steps)}")
        step = {"key": key, "cat": cat, "actor": actor, "email": email.group(0) if email else None, "deadline": deadline}
        if steps and "pending_condition" in steps[-1]:
            step["cond"] = steps.pop()["pending_condition"]
        if cond:
            step["cond"] = cond
        step_fields = []
        if cat in ("start", "task"):
            fm = FIELD_LIST.search(" " + after) or FIELD_LIST.search(s)
            phrase = fm.group(1) if fm else after
            for label in _split_fields(phrase or ""):
                if cat == "task" and len(label.split()) > 5:
                    continue
                step_fields.append(label)
            if not step_fields and cat == "task":
                step_fields = [f"{_title(word.rstrip('s'))} details"] if word not in ("process", "processes") else ["Notes"]
        step["fields"] = step_fields
        if first and not [x for x in steps if "key" in x] and cat != "start":
            pass
        steps.append(step)

    steps = [s for s in steps if "key" in s]
    if not steps:
        steps = [{"key": "requester", "cat": "start", "actor": "Requester", "fields": ["Request details"], "email": None},
                 {"key": "approver", "cat": "approve", "actor": "Approver", "fields": [], "email": None}]
        notes.append("Couldn't find the steps in the description, so a request → approval workflow was started for you.")
    if steps[0]["cat"] != "start":
        steps.insert(0, {"key": "requester", "cat": "start", "actor": "Requester", "fields": ["Request details"], "email": None})
        notes.append("Added a first “Requester” step that starts the workflow.")
    if not steps[0]["fields"]:
        steps[0]["fields"] = ["Request details"]

    first_key = steps[0]["key"]
    used_ids = set()

    def add_field(label, step_key, **extra):
        fid = base = _slug(label)
        n = 2
        while fid in used_ids:
            fid, n = f"{base}_{n}", n + 1
        used_ids.add(fid)
        f = {"id": fid, "type": extra.pop("type", None) or field_type(label), "label": label, "name": label,
             "filled_by": [step_key], **extra}
        if f["type"] in ("text", "email", "currency", "number", "date", "phone", "dropdown", "radio", "signature") and \
                step_key == first_key:
            f.setdefault("required", True)
        if f["type"] == "attachment":
            f.pop("required", None)
        fields.append(f)
        return f

    def find_field(phrase):
        p = phrase.lower().strip()
        for f in fields:
            if f["label"].lower() == p:
                return f
        for f in fields:
            if p in f["label"].lower() or f["label"].lower() in p:
                return f
        return None

    out_steps = []
    for st in steps:
        for label in st["fields"]:
            if not find_field(label) or st["key"] != first_key:
                add_field(label, st["key"])
        if st["cat"] == "start":
            title = "Requester" if not st["actor"] or st["actor"].lower() in ("someone", "anyone", "user") else _title(st["actor"])
        elif st["cat"] == "approve":
            title = f"{_title(st['actor'] or 'Manager')} approval"
        elif st["cat"] == "review":
            title = f"{_title(st['actor'] or 'Reviewer')} review"
        elif st["cat"] == "sign":
            title = f"{_title(st['actor'] or 'Signer')} signature"
        else:
            title = _title(st["actor"] or "Task")
        st["title"] = title
        if st["cat"] == "approve":
            add_field(f"{title} decision", st["key"], type="radio", options=["Approve", "Reject"], required=True)
            add_field(f"{title} comments", st["key"], type="textarea")
        elif st["cat"] == "review":
            add_field(f"{title} outcome", st["key"], type="radio", options=["Complete", "Send back"], required=True)
            add_field(f"{title} notes", st["key"], type="textarea")
        elif st["cat"] == "sign":
            add_field(f"{title}", st["key"], type="signature", required=True)
            add_field(f"{title} date", st["key"], type="date", required=True)
        for f in fields:  # make task fields required for the step that owns them
            if f["filled_by"] == [st["key"]] and st["cat"] == "task" and f["type"] not in ("attachment", "textarea"):
                f.setdefault("required", True)

        spec = {"key": st["key"], "name": title}
        if st["cat"] == "start":
            spec["recipient"] = {"type": "anyone"}
        elif st["email"]:
            spec["recipient"] = {"type": "email", "email": st["email"]}
        else:
            spec["recipient"] = {"type": "group", "group": _group_name(st["actor"] or title)}
        if st.get("cond"):
            phrase, op, value, _ = st["cond"]
            f = find_field(phrase)
            if f is None:
                f = add_field(_title(phrase), first_key)
                if f["type"] == "text" and value.replace(".", "").isdigit():
                    f["type"] = "currency" if any(w in phrase.lower() for w in ("amount", "cost", "price", "total", "value")) else "number"
                notes.append(f"Added “{f['label']}” to the first step so the condition on {title} can be checked.")
            spec["condition"] = {"logic": "and", "rules": [{"field": f["name"], "op": op, "value": value}]}
        if st.get("deadline"):
            spec.setdefault("settings", {})["sla"] = st["deadline"]
        if st["cat"] == "review":
            spec["jumps"] = [{"to": first_key, "when": {"rules": [{"field": f"{title} outcome", "op": "eq", "value": "Send back"}]}}]
            spec.setdefault("settings", {})["recurrence"] = True
        out_steps.append(spec)
        if st["cat"] == "approve":
            out_steps.append({"key": f"{st['key']}_outcome", "type": "branch", "name": f"{title} outcome", "lanes": [
                {"id": "rejected", "label": "Rejected", "target": "end",
                 "when": {"rules": [{"field": f"{title} decision", "op": "eq", "value": "Reject"}]}}]})
    if any(s.get("jumps") for s in out_steps):
        out_steps[0].setdefault("settings", {})["recurrence"] = True

    named = next((f for f in fields if f["filled_by"] == [first_key] and f["type"] in ("text",)), None)
    if named:
        bots.append({"type": "rename_packet", "name": "Name each document", "trigger": "step_complete", "step": first_key,
                     "config": {"pattern": f"{name} – {{field:{named['name']}}}"}})
    if notify_requester:
        email_field = next((f for f in fields if f["type"] == "email" and f["filled_by"] == [first_key]), None)
        if email_field is None:
            email_field = add_field("Email", first_key, type="email", required=True)
            notes.append("Added an “Email” field to the first step so the requester can be emailed at the end.")
        bots.append({"type": "email_on_complete", "name": "Email the requester", "trigger": "flow_complete",
                     "config": {"to": f"{{field:{email_field['name']}}}", "subject": f"Your {name.lower()} is complete",
                                "body": "Hello,\n\nYour request has been completed. The final document is attached.",
                                "attach_pdf": True}})

    return {"name": name, "description": text[:500], "documents": [{"key": "main", "name": f"{name} form", "fields": fields}],
            "steps": out_steps, "bots": bots}, notes
