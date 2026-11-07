"""Builder › AI assistant: the model reads the workflow being edited (as a blueprint), suggests improvements and applies
the change the user asks for. Applying keeps the keys of unchanged steps/documents, the documents' design and files, and
saves the previous definition so the change can be undone. Nothing is published — the user reviews and publishes."""
import copy
import json
import re

from django.utils import timezone

from . import ai_actions, ai_builder, services
from . import blueprint as bp
from .models import Workflow

EDIT_SYSTEM = """You edit an EXISTING Paperlane document workflow. You get its CURRENT blueprint and a change request.
Reply with ONE JSON object only: {"edits": [ ... ]} — a list of edit operations that make the requested change.
Do NOT return the whole blueprint. Refer to steps, fields, documents and bots by their NAME exactly as written in
the outline (e.g. "step": "Legal Team Verification") — not by key.

Operations:
- {"op": "update_step", "step": "<step>", "set": {<properties to change>}}
    step properties: name, subject (invitation email subject), message (invitation email body; tokens {user_name},
    {workflow_name}, {link}), recipient ({"type": "email", "email": ...} | {"type": "group", "group": "<name>"} |
    {"type": "anyone"}), condition ({"logic": "and", "rules": [{"field": "<field name>", "op": "eq", "value": "..."}]}),
    jumps, lanes (branch steps only)
- {"op": "add_step", "after": "<step, or null for first>", "step": {"key": "new_snake_key", "name": "...",
    "type": "step"|"branch", "recipient": {...}, ...}}
- {"op": "remove_step", "step": "<step>"}
- {"op": "add_field", "document": "<document, optional>", "field": {"id": "snake_id", "type": "<field type>", "label": "...",
    "required": true|false, "options": [...], "filled_by": ["<step>"]}}
- {"op": "update_field", "field": "<field label or id>", "set": {"required": true, "label": "...", "options": [...],
    "filled_by": ["<step>"], ...}}
- {"op": "remove_field", "field": "<field>"}
- {"op": "add_bot", "bot": {"type": "<bot type>", "name": "...", "trigger": "step_complete|flow_complete|step_start",
    "step": "<step, optional>", "config": {...}}}
- {"op": "update_bot", "bot": "<bot name>", "set": {...}}   - {"op": "remove_bot", "bot": "<bot name>"}
- {"op": "rename_workflow", "name": "..."}

Rules:
- Edit ONLY the steps, fields and bots the request is about. Never touch anything the request does not mention —
  e.g. "update the Legal step's email" is one update_step for that step only.
- Include what the change requires (a new approver needs a required radio "<Step> decision" field with
  Approve/Reject filled by that step, and a branch that ends the workflow on Reject).
- Never return an empty list."""

SUGGEST_SYSTEM = """You review document workflows built in Paperlane and suggest concrete improvements: missing approvals
or checks, required fields that should be required, missing notifications, risky routing, unclear step names, fields
nobody fills, automation that would save time (bots: rename, tags, emails, AI review). Reply with ONE JSON object:
{"suggestions": [{"title": "short title", "why": "one sentence", "request": "the change, phrased as an instruction"}]}
with 3 to 6 suggestions, most valuable first. Base them only on the blueprint. Refer to steps, documents and fields by
their names — never by keys or ids."""


def current_blueprint(wf):
    data = bp.export_workflow(wf)
    for k in ("id", "status", "start_forms", "autostart"):
        data.pop(k, None)
    return data


def outline(data):
    """A readable outline (names, not keys) that small models can follow."""
    names = {st["key"]: st.get("name") for lst in _step_lists(copy.deepcopy(data.get("steps") or [])) for st in lst}
    lines = ["STEPS (in order):"]

    def walk(steps, depth=0):
        for st in steps:
            pad = "   " * depth
            if st.get("type") == "branch":
                lines.append(f"{pad}- Branch “{st['name']}”")
                for ln in st.get("lanes") or []:
                    rules = ", ".join(f"{r.get('field')} {r.get('op')} {r.get('value', '')}".strip() for r in (ln.get("when") or {}).get("rules", []))
                    lines.append(f"{pad}   • lane “{ln.get('label')}” when {rules or 'always'} → {names.get(ln.get('target'), ln.get('target'))}")
                    walk(ln.get("steps") or [], depth + 2)
                continue
            r = st.get("recipient") or {}
            who = r.get("email") or r.get("group") or r.get("type", "")
            cond = ", ".join(f"{c.get('field')} {c.get('op')} {c.get('value', '')}" for c in (st.get("condition") or {}).get("rules", []))
            lines.append(f"{pad}- Step “{st['name']}” → {who}{f' (only when {cond})' if cond else ''}; "
                         f"email subject: “{(st.get('subject') or '')[:80]}”")
    walk(data.get("steps") or [])
    for d in data.get("documents") or []:
        lines.append(f"DOCUMENT “{d.get('name')}” fields:")
        for f in d.get("fields") or []:
            by = f.get("filled_by")
            by = ", ".join(names.get(k, k) for k in by) if isinstance(by, list) else (by or "anyone")
            lines.append(f"- “{f.get('label') or f.get('id')}” ({f.get('type')}{', required' if f.get('required') else ''}) filled by {by}")
    lines.append("BOTS: " + (", ".join(f"“{b.get('name')}” ({b.get('type')}, {b.get('trigger')})" for b in data.get("bots") or []) or "none"))
    return "\n".join(lines)


EXPLAIN_SYSTEM = """You explain a document workflow to the people who take part in it — managers, approvers, auditors —
who have never seen the builder. Use plain words: no keys, ids, JSON or builder jargon ("lane", "bot", "blueprint").
Say "the system" for automatic actions. Reply with ONE JSON object:
{"summary": "two or three sentences: what the workflow is for and how it ends",
 "steps": [{"who": "role or person", "does": "what they do, in one sentence"}],
 "decisions": ["each routing rule as a plain sentence, e.g. Claims over 10,000 go to a senior assessor"],
 "automatic": ["each automatic action as a plain sentence, e.g. The claimant gets an email when it is rejected"],
 "deadlines": ["each deadline or reminder as a plain sentence"]}
Follow the order of the steps. Only describe what the workflow actually contains."""


def explain(wf, user=None, refresh=False):
    """A plain-English description of the workflow, cached until the definition changes."""
    import hashlib

    data = current_blueprint(wf)
    sig = hashlib.sha1(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]
    cached = (wf.options or {}).get("ai_explain") or {}
    if cached.get("sig") == sig and not refresh:
        return cached
    engine = ai_actions.engine_for(wf.workspace)
    raw = ai_actions.chat_json(wf.workspace, engine, EXPLAIN_SYSTEM, f"Workflow “{data.get('name')}”\n\n{outline(data)}",
                               kind="workflow_explain", user=user)

    def lines(key, n=12):
        return [str(x).strip()[:300] for x in (raw.get(key) or []) if isinstance(x, str) and str(x).strip()][:n]
    steps = [{"who": str(x.get("who") or "")[:80], "does": str(x.get("does") or "")[:300]}
             for x in (raw.get("steps") or []) if isinstance(x, dict) and x.get("does")][:20]
    out = {"sig": sig, "summary": str(raw.get("summary") or "")[:800], "steps": steps, "decisions": lines("decisions"),
           "automatic": lines("automatic"), "deadlines": lines("deadlines"), "at": timezone.now().isoformat(),
           "engine": ai_actions.ai_providers.label(engine)}
    wf.options = {**(wf.options or {}), "ai_explain": out}
    wf.save(update_fields=["options"])
    return out


def explain_text(ex, name):
    parts = [name, "", ex.get("summary", ""), ""]
    if ex.get("steps"):
        parts += ["Who does what:"] + [f"{i}. {s['who']}: {s['does']}" for i, s in enumerate(ex["steps"], 1)] + [""]
    for title, key in (("Decisions", "decisions"), ("Automatic", "automatic"), ("Deadlines", "deadlines")):
        if ex.get(key):
            parts += [f"{title}:"] + [f"- {x}" for x in ex[key]] + [""]
    return "\n".join(parts).strip()


def examples(wf, limit=4):
    """Change requests that fit this workflow: they name its real steps and fields, so a click gives a useful start."""
    from . import checks

    steps = [s for s in wf.ordered_steps() if s.kind == "recipient"]
    fields = [f for d in wf.documents.all() for f in d.input_fields()]
    def label(f):
        text = str(f.get("label") or f.get("name") or f["id"]).strip().strip('"')
        return f"“{text[:40]}{'…' if len(text) > 40 else ''}”"

    money = next((f for f in fields if f.get("type") in ("currency", "number")), None)
    email = next((f for f in fields if f.get("type") == "email"), None)
    choice = next((f for f in fields if f.get("type") in ("radio", "dropdown") and f.get("options")), None)
    out = [f["fix"] for f in checks.run(wf) if f.get("fix") and f["level"] != checks.INFO][:1]
    if money and steps:
        out.append(f"Add a senior approval after “{steps[-1].name}” when {label(money)} is over 10,000")
    if email:
        out.append(f"When the workflow completes, email the address in {label(email)} a summary of the decision")
    if len(steps) > 1:
        out.append(f"Remind “{steps[1].name}” every day if they haven't acted, and escalate after 3 business days")
    if choice:
        out.append(f"Tag documents with the value of {label(choice)} when the workflow completes")
    if not any(f.get("type") == "signature" for f in fields) and steps:
        out.append(f"Add a required signature for “{steps[-1].name}”")
    generic = ["Make every contact field required", "Add an AI review that checks the amounts",
               "Email the requester when it is declined", "Tag completed documents with the department"]
    out += [g for g in generic if g not in out]
    return out[:limit]


def _context_text(wf, data):
    extra = {"published": wf.status == Workflow.PUBLISHED, "documents_started": wf.packets.count(),
             "start_forms": [f.name for f in wf.intake_forms.filter(archived_at__isnull=True)]}
    return (f"Workflow “{data.get('name')}” — facts: {json.dumps(extra)}\n\n{outline(data)}\n\n"
            f"CURRENT blueprint (for details):\n{json.dumps(data, ensure_ascii=False)}")


def suggest(wf, user=None, engine=""):
    """3–6 improvements. The automatic checks go first in the prompt so the model explains and fixes real problems
    before inventing new ones; the JSON reply gets one self-repair retry."""
    from . import checks

    engine = ai_actions.engine_for(wf.workspace, engine)
    found = [f for f in checks.run(wf) if f["level"] != checks.INFO]
    prompt = (_context_text(wf, current_blueprint(wf))
              + "\n\nAUTOMATIC CHECKS found these problems (address the important ones first; don't repeat ones that "
                "need a person outside the workflow, like adding group members):\n" + checks.as_text(found))
    data = ai_actions.chat_json(wf.workspace, engine, SUGGEST_SYSTEM, prompt, kind="workflow_suggest", user=user)
    out, seen = [], set()
    for s in (data.get("suggestions") or [])[:8]:
        if isinstance(s, dict) and s.get("request") and str(s["request"]).strip().lower() not in seen:
            seen.add(str(s["request"]).strip().lower())
            out.append({"title": str(s.get("title") or s["request"])[:120], "why": str(s.get("why") or "")[:300],
                        "request": str(s["request"])[:600]})
    return out


def propose(wf, request_text, user=None, engine="", start=None, earlier=""):
    """Ask the model for edit operations, apply them to the current blueprint and check the result.

    ``start`` refines a pending proposal instead (a follow-up like "make it 5,000"): the edits apply on top of it and
    the reported diff is still against the saved workflow, so Apply shows everything that will change.
    Returns {blueprint, changes, diff, preview, engine_label}. Retries once when the edits fail or change nothing."""
    engine = ai_actions.engine_for(wf.workspace, engine)
    base = _normalize(current_blueprint(wf))[0]
    before = copy.deepcopy(start) if start else current_blueprint(wf)
    start_norm = _normalize(before)[0] if start else base
    system = EDIT_SYSTEM + "\n\nFIELD TYPES, OPERATORS, BOTS: " + json.dumps(ai_builder._compact_guide())
    lead = (f"This is a PROPOSED version that already includes an earlier change (“{earlier[:300]}”). "
            "Adjust it as asked; keep the earlier change unless the request undoes it.\n\n") if start else ""
    messages = [{"role": "user", "content": lead + _context_text(wf, before) + f"\n\nChange request: {request_text}"}]
    last = ""
    for attempt in range(2):
        reply = ai_actions.chat(wf.workspace, engine, system, messages, kind="workflow_edit", user=user)
        try:
            raw = ai_builder.extract_json(reply)
            edits = raw.get("edits") if isinstance(raw.get("edits"), list) else ([raw] if raw.get("op") else [])
            if not edits and raw.get("steps") and raw.get("documents"):  # the model returned a whole blueprint anyway
                changes = [str(c)[:300] for c in (raw.pop("changes", None) or []) if str(c).strip()][:12]
                edited, errors = raw, []
            else:
                edited, changes, errors = apply_edits(before, _drop_copies(before, edits, request_text))
            blueprint, notes = _normalize(edited)
            changed = diff(base, blueprint)
            if not diff(start_norm, blueprint):
                raise bp.BlueprintError("; ".join(errors[:4]) or "the edits changed nothing")
            preview = ai_builder.dry_run(wf.workspace, user, blueprint)
            if attempt == 0 and preview.get("problems"):
                raise bp.BlueprintError("; ".join(preview["problems"][:4]))
            from . import checks

            known = {f["message"] for f in checks.run(wf)}
            preview["new_warnings"] = [w for w in preview.get("warnings", []) if w not in known][:6]
            return {"blueprint": blueprint, "changes": changes + [f"Skipped: {e}" for e in errors], "notes": notes,
                    "diff": changed, "preview": preview, "engine_label": ai_actions.ai_providers.label(engine)}
        except (ValueError, bp.BlueprintError) as exc:
            last = str(exc)
            ai_actions.note_failure(wf.workspace, last, reply)
            messages += [{"role": "assistant", "content": reply[:6000]},
                         {"role": "user", "content": f"Those edits did not work: {last}.\nChange request again: {request_text}\n"
                                                     'Reply with {"edits": [...]} that actually make this change.'}]
    raise ai_actions.AIError(f"The model didn't produce a working change ({last}). Try rephrasing, or pick a larger model.")


def _drop_copies(data, edits, request_text):
    """Small models sometimes paste one update onto every step. When an identical update targets a step the request
    names and also steps it doesn't, keep only the named ones."""
    text = _norm(request_text)
    names = {}
    for lst in _step_lists(copy.deepcopy(data.get("steps") or [])):
        for st in lst:
            names[_norm(st["key"])] = names[_norm(st.get("name"))] = _norm(st.get("name"))
    groups = {}
    for e in edits:
        if isinstance(e, dict) and e.get("op") == "update_step":
            groups.setdefault(json.dumps(e.get("set"), sort_keys=True, default=str), []).append(e)
    drop = set()
    for same in groups.values():
        named = [e for e in same if names.get(_norm(e.get("step")), "") and names[_norm(e.get("step"))] in text]
        if len(same) > 1 and named:
            drop.update(id(e) for e in same if e not in named)
    return [e for e in edits if id(e) not in drop]


# ---- edit operations ---------------------------------------------------------------------------------------------
def _norm(x):
    return str(x or "").strip().strip("“”\"'").lower()


def _step_lists(steps, out=None):
    """Every list that holds steps (the top level and each branch lane)."""
    out = [] if out is None else out
    out.append(steps)
    for st in steps:
        for ln in st.get("lanes") or []:
            ln.setdefault("steps", [])
            _step_lists(ln["steps"], out)
    return out


def _find_step(spec, ref):
    """By key, current name, or the name it had before this batch of edits (spec["_names"])."""
    want = _norm(ref)
    key = (spec.get("_names") or {}).get(want)
    for lst in _step_lists(spec["steps"]):
        for i, st in enumerate(lst):
            if want in (_norm(st.get("key")), _norm(st.get("name"))):
                return lst, i
    for lst in _step_lists(spec["steps"]):
        for i, st in enumerate(lst):
            if key and st.get("key") == key:
                return lst, i
    return None, None


def _step_key(spec, ref):
    lst, i = _find_step(spec, ref)
    return lst[i]["key"] if lst is not None else ref


def _find_field(spec, ref):
    for d in spec["documents"]:
        for i, f in enumerate(d.get("fields") or []):
            if _norm(ref) in (_norm(f.get("id")), _norm(f.get("label")), _norm(f.get("name"))):
                return d, i
    return None, None


def _fix_filled_by(spec, f):
    if isinstance(f.get("filled_by"), (str, list)) and f["filled_by"] != "anyone":
        refs = f["filled_by"] if isinstance(f["filled_by"], list) else [f["filled_by"]]
        f["filled_by"] = [_step_key(spec, r) for r in refs]


def apply_edits(blueprint, edits):
    """Apply edit operations to a copy of ``blueprint``. Returns (blueprint, change sentences, errors)."""
    spec = copy.deepcopy(blueprint)
    spec.setdefault("steps", [])
    spec.setdefault("bots", [])
    spec["_names"] = {_norm(st.get("name")): st["key"] for lst in _step_lists(spec["steps"]) for st in lst}
    changes, errors = [], []
    for e in edits[:40]:
        if not isinstance(e, dict):
            continue
        op = str(e.get("op") or "").lower()
        sets = e.get("set") if isinstance(e.get("set"), dict) else {}
        try:
            if op == "update_step":
                lst, i = _find_step(spec, e.get("step"))
                if lst is None:
                    raise ValueError(f"no step “{e.get('step')}”")
                st = lst[i]
                for k, v in sets.items():
                    k = {"message_subject": "subject", "email_subject": "subject", "message_body": "message",
                         "email_body": "message", "body": "message"}.get(k, k)
                    if k in ("key", "type"):
                        continue
                    st[k] = v
                changes.append(f"Updated step “{st['name']}”: " + ", ".join(sets))
            elif op == "add_step":
                new = dict(e.get("step") or {})
                if not new.get("name"):
                    raise ValueError("add_step needs a step name")
                new.setdefault("key", re.sub(r"\W+", "_", new["name"].lower()).strip("_")[:40] or "new_step")
                new.setdefault("type", "step")
                if e.get("after") in (None, "", "start"):
                    spec["steps"].insert(0, new)
                else:
                    lst, i = _find_step(spec, e["after"])
                    if lst is None:
                        raise ValueError(f"no step “{e['after']}” to add after")
                    lst.insert(i + 1, new)
                changes.append(f"Added step “{new['name']}”")
            elif op == "remove_step":
                lst, i = _find_step(spec, e.get("step"))
                if lst is None:
                    raise ValueError(f"no step “{e.get('step')}”")
                gone = lst.pop(i)
                for d in spec["documents"]:
                    for f in d.get("fields") or []:
                        if isinstance(f.get("filled_by"), list) and gone["key"] in f["filled_by"]:
                            f["filled_by"] = [k for k in f["filled_by"] if k != gone["key"]] or "anyone"
                changes.append(f"Removed step “{gone.get('name')}”")
            elif op == "add_field":
                f = dict(e.get("field") or {})
                if not f.get("label") and not f.get("id"):
                    raise ValueError("add_field needs a label")
                f.setdefault("label", f.get("id"))
                f.setdefault("id", re.sub(r"\W+", "_", f["label"].lower()).strip("_")[:40])
                f.setdefault("type", "text")
                _fix_filled_by(spec, f)
                doc = next((d for d in spec["documents"] if e.get("document") and _norm(e["document"]) in
                            (_norm(d.get("key")), _norm(d.get("name")))), spec["documents"][0])
                doc.setdefault("fields", []).append(f)
                changes.append(f"Added field “{f['label']}” to {doc.get('name')}")
            elif op == "update_field":
                d, i = _find_field(spec, e.get("field"))
                if d is None:
                    raise ValueError(f"no field “{e.get('field')}”")
                f = d["fields"][i]
                f.update({k: v for k, v in sets.items() if k != "id"})
                _fix_filled_by(spec, f)
                changes.append(f"Updated field “{f.get('label')}”: " + ", ".join(sets))
            elif op == "remove_field":
                d, i = _find_field(spec, e.get("field"))
                if d is None:
                    raise ValueError(f"no field “{e.get('field')}”")
                changes.append(f"Removed field “{d['fields'].pop(i).get('label')}”")
            elif op == "add_bot":
                b = dict(e.get("bot") or {})
                if not b.get("type"):
                    raise ValueError("add_bot needs a type")
                if b.get("step"):
                    b["step"] = _step_key(spec, b["step"])
                spec["bots"].append(b)
                changes.append(f"Added bot “{b.get('name') or b['type']}”")
            elif op in ("update_bot", "remove_bot"):
                i = next((i for i, b in enumerate(spec["bots"]) if _norm(e.get("bot")) in (_norm(b.get("name")), _norm(b.get("type")))), None)
                if i is None:
                    raise ValueError(f"no bot “{e.get('bot')}”")
                if op == "remove_bot":
                    changes.append(f"Removed bot “{spec['bots'].pop(i).get('name')}”")
                else:
                    spec["bots"][i].update({k: (_step_key(spec, v) if k == "step" else v) for k, v in sets.items()})
                    changes.append(f"Updated bot “{spec['bots'][i].get('name')}”: " + ", ".join(sets))
            elif op == "rename_workflow" and e.get("name"):
                spec["name"] = str(e["name"])[:200]
                changes.append(f"Renamed the workflow to “{spec['name']}”")
            else:
                raise ValueError(f"unknown operation “{op}”")
        except (ValueError, TypeError, AttributeError) as exc:
            errors.append(str(exc))
    for d in spec["documents"]:  # steps added later in the batch can now be resolved
        for f in d.get("fields") or []:
            _fix_filled_by(spec, f)
    spec.pop("_names", None)
    return spec, changes, errors


def _normalize(raw):
    first = copy.deepcopy(((raw.get("steps") or [{}])[0] or {}).get("recipient"))
    blueprint, notes = ai_builder.normalize(raw)
    if isinstance(first, dict) and blueprint["steps"] and blueprint["steps"][0]["type"] == "step":
        blueprint["steps"][0]["recipient"] = first  # normalize() resets the first step to "anyone"
    return blueprint, notes


# ---- diff -------------------------------------------------------------------------------------------------------
def _steps(spec):
    return {s["key"]: s for s, _ in bp._flatten_steps(spec.get("steps") or [])}


def _fields(spec):
    return {(d.get("key"), f.get("id")): (d, f) for d in spec.get("documents") or [] for f in d.get("fields") or []}


def _bots(spec):
    return {(b.get("type"), b.get("name")): b for b in spec.get("bots") or []}


_LANE_FACE = ("label", "when", "target", "then")


def _lanes(spec):
    """Branch lanes by (step key, lane id), compared on label, when, target and then. Nested steps stay in the step diff."""
    out = {}
    try:
        flat = bp._flatten_steps(spec.get("steps") or [])
    except bp.BlueprintError:
        return out
    for st, _lane in flat:
        if (st.get("type") or "step") != "branch":
            continue
        branch = st.get("name") or st.get("key")
        for i, ln in enumerate(st.get("lanes") or []):
            if not isinstance(ln, dict):
                continue
            lid = str(ln.get("id") or ln.get("label") or f"lane{i + 1}")
            face = {k: ln.get(k) for k in _LANE_FACE}
            out[(st.get("key"), lid)] = {"key": st.get("key"), "name": f"{face.get('label') or lid} ({branch})", "face": face}
    return out


def diff(old, new):
    """[{op: added|removed|changed, what, name}] — steps, branch lanes, fields and bots."""
    out = []

    def cmp(kind, a, b, label):
        step_key = (lambda k: {"key": k}) if kind == "Step" else (lambda k: {})
        for k in b.keys() - a.keys():
            out.append({"op": "added", "what": kind, "name": label(b[k]), **step_key(k)})
        for k in a.keys() - b.keys():
            out.append({"op": "removed", "what": kind, "name": label(a[k]), **step_key(k)})
        for k in a.keys() & b.keys():
            x, y = (a[k][1], b[k][1]) if isinstance(a[k], tuple) else (a[k], b[k])
            props = sorted(p for p in set(x) | set(y) if p not in ("lanes", "steps")
                           and json.dumps(x.get(p), sort_keys=True, default=str) != json.dumps(y.get(p), sort_keys=True, default=str))
            if props:
                out.append({"op": "changed", "what": kind, "name": label(b[k]), "props": props, **step_key(k)})

    cmp("Step", _steps(old), _steps(new), lambda s: s.get("name") or s.get("key"))
    old_l, new_l = _lanes(old), _lanes(new)
    for k in new_l.keys() - old_l.keys():
        out.append({"op": "added", "what": "Lane", "name": new_l[k]["name"], "key": new_l[k]["key"]})
    for k in old_l.keys() - new_l.keys():
        out.append({"op": "removed", "what": "Lane", "name": old_l[k]["name"], "key": old_l[k]["key"]})
    for k in old_l.keys() & new_l.keys():
        props = [p for p in _LANE_FACE
                 if json.dumps(old_l[k]["face"].get(p), sort_keys=True, default=str)
                 != json.dumps(new_l[k]["face"].get(p), sort_keys=True, default=str)]
        if props:
            out.append({"op": "changed", "what": "Lane", "name": new_l[k]["name"], "props": props, "key": new_l[k]["key"]})
    cmp("Field", _fields(old), _fields(new), lambda df: f"{df[1].get('label') or df[1].get('id')} ({df[0].get('name')})")
    cmp("Bot", _bots(old), _bots(new), lambda b: b.get("name") or b.get("type"))
    if old.get("name") != new.get("name"):
        out.append({"op": "changed", "what": "Name", "name": new.get("name")})
    order = {"added": 0, "changed": 1, "removed": 2}
    return sorted(out, key=lambda d: (order[d["op"]], d["what"]))


# ---- "after" preview on the builder canvas --------------------------------------------------------------------
class _Rollback(Exception):
    pass


def render_after(request, wf, plan):
    """Render the builder canvas for the proposed blueprint (built inside a rolled-back transaction).

    Returns {"html", "marks": {step key in the html: "added"|"changed"}, "removed": [current step keys]}."""
    from django.db import transaction
    from django.template.loader import render_to_string

    from .views.builder import canvas_context

    blueprint = plan["blueprint"]
    ops = {d["key"]: d["op"] for d in plan.get("diff", []) if d.get("key")}
    out = {}
    try:
        with transaction.atomic():
            tmp, _ = bp.build_workflow(wf.workspace, request.user, {**blueprint, "autostart": None, "start_form": None},
                                       publish=False, strict=False)
            marks, keys = {}, {}
            for (s, _), st in zip(bp._flatten_steps(blueprint.get("steps") or []), tmp.ordered_steps()):
                keys[s["key"]] = st.key
                if ops.get(s["key"]) in ("added", "changed"):
                    marks[st.key] = ops[s["key"]]
            ctx = {**canvas_context(tmp), "preview": True}
            out = {"html": render_to_string("flows/partials/builder_flow.html", ctx, request=request), "marks": marks, "keys": keys,
                   "removed": [k for k, op in ops.items() if op == "removed"]}
            raise _Rollback
    except _Rollback:
        pass
    return out


# ---- apply / undo -----------------------------------------------------------------------------------------------
def apply(wf, blueprint, user, request_text=""):
    """Replace the workflow's draft definition with ``blueprint``. Returns the validation problems (if any)."""
    from django.db import transaction

    old_snapshot = wf.export_snapshot()
    old_name, old_description = wf.name, wf.description
    old_steps = {s["key"] for s in old_snapshot["steps"]}
    old_docs = {d.key: d for d in wf.documents.all()}
    old_bots = {(b["bot_type"], b["name"]): b for b in old_snapshot["bots"]}
    spec = {**blueprint, "autostart": None, "start_form": None}
    with transaction.atomic():
        tmp, _ = bp.build_workflow(wf.workspace, user, spec, publish=False, strict=False)
        snap = tmp.export_snapshot()
        mapping = {}
        for (s, _), st in zip(bp._flatten_steps(spec.get("steps") or []), tmp.ordered_steps()):
            if s["key"] in old_steps:
                mapping[st.key] = s["key"]
        for d, doc in zip(spec.get("documents") or [], tmp.documents.order_by("order", "id")):
            if d.get("key") in old_docs:
                mapping[doc.key] = d["key"]
        tmp.delete()
        snap = services._remap(snap, mapping)
        for d in snap["documents"]:
            orig = old_docs.get(d["key"])
            if orig is not None:
                d.update(kind=orig.kind, design=orig.design or {}, condition=orig.condition or {})
        for b in snap["bots"]:
            orig = old_bots.get((b["bot_type"], b["name"]))
            if orig:
                for k in ("run_once", "success_tags", "delay_minutes"):
                    b[k] = b.get(k) or orig.get(k)
        services.load_snapshot(wf, snap)
        for doc in wf.documents.all():
            orig = old_docs.get(doc.key)
            if orig is not None and orig.file:
                doc.file = orig.file
                doc.save(update_fields=["file"])
        wf.name = str(blueprint.get("name") or wf.name)[:200]
        wf.description = str(blueprint.get("description") or wf.description)[:500]
        wf.options = {**(wf.options or {}), "ai_undo": {"snapshot": old_snapshot, "name": old_name, "description": old_description,
                                                        "at": timezone.now().isoformat(), "request": request_text[:300]}}
        wf.has_unpublished_changes = True
        wf.save()
    from .views.builder import validate

    return validate(wf)


def undo(wf):
    data = (wf.options or {}).pop("ai_undo", None)
    if not data:
        return False
    services.load_snapshot(wf, data["snapshot"])
    wf.name, wf.description = data.get("name") or wf.name, data.get("description", wf.description)
    wf.save(update_fields=["options", "name", "description"])
    return True
