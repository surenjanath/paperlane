"""Compare two versions of a workflow (published snapshots or the current draft): steps, fields and bots."""
import json

STEP_PROPS = {"name": "name", "recipient_type": "recipient", "recipient_email": "recipient", "recipient_group": "recipient",
              "recipient_contact": "recipient", "condition": "condition", "branches": "branches", "settings": "settings",
              "document_access": "document access", "message_subject": "email", "message_body": "email", "thank_you": "thank-you",
              "order": "position", "kind": "type"}
FIELD_SKIP = {"x", "y", "page"}
BOT_PROPS = {"name": "name", "trigger": "trigger", "step_key": "step", "config": "settings", "condition": "condition",
             "enabled": "on/off", "delay_minutes": "delay", "run_once": "run once", "success_tags": "tags"}


def _j(v):
    return json.dumps(v, sort_keys=True, default=str)


def _cmp(kind, a, b, label, props=None):
    out = []
    for k in b.keys() - a.keys():
        out.append({"op": "added", "what": kind, "name": label(b[k])})
    for k in a.keys() - b.keys():
        out.append({"op": "removed", "what": kind, "name": label(a[k])})
    for k in a.keys() & b.keys():
        x, y = a[k], b[k]
        changed = sorted({(props or {}).get(p, p) for p in set(x) | set(y)
                          if p not in FIELD_SKIP and (props is None or p in props) and _j(x.get(p)) != _j(y.get(p))})
        if changed:
            out.append({"op": "changed", "what": kind, "name": label(y), "props": changed})
    return out


def diff(old, new):
    """[{op, what, name, props?}] between two snapshots (Workflow.build_snapshot / WorkflowVersion.snapshot)."""
    steps = lambda s: {x["key"]: x for x in s.get("steps", [])}  # noqa: E731
    fields = lambda s: {(d["key"], f["id"]): {**f, "_doc": d.get("name")} for d in s.get("documents", []) for f in d.get("fields", [])}  # noqa: E731
    docs = lambda s: {d["key"]: {"name": d.get("name"), "condition": d.get("condition")} for d in s.get("documents", [])}  # noqa: E731
    bots = lambda s: {b["key"]: b for b in s.get("bots", [])}  # noqa: E731
    old_steps, new_steps = steps(old), steps(new)
    # "order" numbers are renumbered whenever a step is inserted, so compare each step's place among the steps both
    # versions share: only a step that really moved reports "position"
    common = old_steps.keys() & new_steps.keys()
    rank = lambda m: {k: i for i, k in enumerate(sorted((k for k in m if k in common), key=lambda k: (m[k].get("order", 0), k)))}  # noqa: E731
    old_rank, new_rank = rank(old_steps), rank(new_steps)
    old_steps = {k: {**v, "order": old_rank.get(k)} for k, v in old_steps.items()}
    new_steps = {k: {**v, "order": new_rank.get(k)} for k, v in new_steps.items()}
    out = _cmp("Step", old_steps, new_steps, lambda s: s.get("name") or s["key"], STEP_PROPS)
    out += _cmp("Document", docs(old), docs(new), lambda d: d.get("name"))
    out += _cmp("Field", fields(old), fields(new), lambda f: f"{f.get('label') or f.get('name') or f['id']} ({f.get('_doc')})")
    out += _cmp("Bot", bots(old), bots(new), lambda b: b.get("name") or b.get("bot_type"), BOT_PROPS)
    if old.get("name") != new.get("name"):
        out.append({"op": "changed", "what": "Workflow", "name": new.get("name"), "props": ["name"]})
    order = {"added": 0, "changed": 1, "removed": 2}
    return sorted(out, key=lambda d: (order[d["op"]], ["Workflow", "Step", "Document", "Field", "Bot"].index(d["what"])))
