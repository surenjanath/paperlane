"""Workflow simulator: run test documents through every branch before publishing.

Each scenario is a real run of the engine on a test document inside a transaction that is always rolled back, so nothing
is kept. While simulating, emails are recorded instead of sent and bots that reach outside services (connectors, AI,
storage, chat, webhooks) are recorded as "would run" instead of executed. Scenarios come from the workflow's own
conditions: one baseline, then one variation per value that flips a branch, jump or step condition."""
import contextlib
import contextvars
import datetime as dt
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from .fieldtypes import is_input

SIMULATING = contextvars.ContextVar("paperlane_simulating", default=None)  # a dict collecting what would happen
COMPUTED = contextvars.ContextVar("paperlane_sim_computed", default=frozenset())
SAFE_BOT_CATEGORIES = {"Fields data", "Tags", "Permissions", "Documents"}  # local data only: these really run
SIM_SKIP_TYPES = {"save_to_storage", "start_workflow", "attach_certificate"}  # local but slow or spawning: recorded only
MAX_SCENARIOS = 80
MAX_ACTIVATIONS = 40
LOOP_LIMIT = 3  # the same step opening this many times in one scenario is reported as a loop


@contextlib.contextmanager
def simulating():
    log = {"emails": [], "bots": []}
    token = SIMULATING.set(log)
    try:
        yield log
    finally:
        SIMULATING.reset(token)


# ---- scenario generation ---------------------------------------------------------------------------------------
def _num(v):
    try:
        return Decimal(str(v).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _fmt(n):
    return str(int(n)) if n == n.to_integral_value() else str(n)


def _conditions(snapshot):
    """(where, condition) for every routing decision: step conditions, branch lanes and jumps."""
    out = []
    for s in snapshot.get("steps", []):
        if s.get("condition"):
            out.append((f"Step “{s['name']}”", s["condition"]))
        for br in s.get("branches") or []:
            out.append((f"{s['name']}: {br.get('label') or 'lane'}", {"logic": br.get("logic", "and"), "rules": br.get("rules") or []}))
        for j in (s.get("settings") or {}).get("jumps") or []:
            out.append((f"Jump after {s['name']}", {"logic": j.get("logic", "and"), "rules": j.get("rules") or []}))
    for b in snapshot.get("bots", []):  # diagram bots route and calculate on their own conditions
        if b.get("condition"):
            out.append((f"Bot “{b.get('name') or b['bot_type']}”", b["condition"]))
    return out


REF = __import__("re").compile(r"\{(?:field:)?([^{}]+)\}")


def _computed(snapshot):
    """{field reference: [input references]} for formula fields and calculation-bot targets."""
    out = {}
    for d in snapshot.get("documents", []):
        for f in d.get("fields", []):
            expr = f.get("formula") or ""
            if f.get("type") == "formula" and expr:
                for k in (f["id"], f.get("name"), f.get("label")):
                    if k:
                        out.setdefault(k, []).extend(REF.findall(expr))
    for b in snapshot.get("bots", []):
        if b.get("bot_type") == "calculate":
            for line in str((b.get("config") or {}).get("mapping") or "").splitlines():
                if "=" in line:
                    target, expr = line.split("=", 1)
                    out.setdefault(target.strip(), []).extend(REF.findall(expr))
    return out


def _leaves(ref, computed, seen=None):
    seen = seen or set()
    if ref in seen:
        return set()
    seen.add(ref)
    if ref not in computed:
        return {ref}
    out = set()
    for x in computed[ref]:
        out |= _leaves(x.strip(), computed, seen)
    return out


def _rules(cond):
    for r in (cond or {}).get("rules", []):
        if isinstance(r, dict) and "rules" in r:
            yield from _rules(r)
        elif isinstance(r, dict) and r.get("field"):
            yield r


def _field_index(snapshot):
    """{reference: (doc_key, field)} for ids, names and labels."""
    idx = {}
    for d in snapshot.get("documents", []):
        for f in d.get("fields", []):
            if not is_input(f.get("type")):
                continue
            for k in (f["id"], f.get("name"), f.get("label")):
                if k and k not in idx:
                    idx[k] = (d["key"], f)
            if f.get("type") == "checkbox" and f.get("name") and f.get("options"):  # "<name>-<option>" keys
                for label, key in zip(f["options"], f.get("option_values") or f["options"]):
                    idx.setdefault(f"{f['name']}-{key}", (d["key"], {**f, "_option": label}))
    return idx


def baseline_value(f):
    t = f.get("type")
    opts = f.get("options") or []
    if t in ("radio", "dropdown", "select"):
        return opts[0] if opts else "Option 1"
    if t in ("checkbox", "checkboxes", "multiselect"):
        return [opts[0]] if opts else True
    if t in ("number", "currency", "formula"):
        return "100"
    if t == "email":
        return "simulated@example.com"
    if t == "phone":
        return "+1 555 0100"
    if t in ("date",):
        return timezone.localdate().isoformat()
    if t in ("datetime",):
        return timezone.now().strftime("%Y-%m-%dT%H:%M")
    if t == "time":
        return "10:00"
    if t in ("signature", "initials"):
        return "simulated-signature"
    if t == "attachment":
        return [{"name": "simulated.pdf", "file_id": 0}]
    if t == "clickwrap":
        return True
    if t == "table":
        return [{c: "Test" for c in (f.get("columns") or ["Item"])}]
    if t == "full_name":
        return "Sam Simulated"
    if t == "address":
        return "1 Test Street"
    return "Test"


def candidates(rule, f):
    """Values for field ``f`` that make ``rule`` true and false."""
    op, want = rule.get("op", "eq"), rule.get("value", "")
    if f.get("_option"):  # a single checkbox option: ticked or not
        return [[f["_option"]], []]
    opts = [o for o in (f.get("options") or []) if str(o).strip()]
    if op in ("eq", "neq", "contains", "not_contains"):
        other = next((o for o in opts if str(o).strip().lower() != str(want).strip().lower()), None)
        if other is None:
            n = _num(want)
            other = _fmt(n + 1) if n is not None else f"not {want}"
        return [str(want), str(other)]
    if op in ("gt", "gte", "lt", "lte"):
        n = _num(want)
        if n is None:
            return []
        return [_fmt(n - 1), _fmt(n), _fmt(n + 1)]
    if op in ("empty", "not_empty", "checked", "not_checked"):
        return ["", baseline_value(f) if f.get("type") not in ("checkbox", "clickwrap") else True]
    if op in ("after", "until"):
        try:
            d = dt.date.fromisoformat(str(want)[:10])
        except ValueError:
            return []
        return [(d - dt.timedelta(days=1)).isoformat(), (d + dt.timedelta(days=1)).isoformat()]
    return []


def truthy(rule, f):
    """One value for field ``f`` that makes ``rule`` true, or None when it can't be built."""
    op, want = rule.get("op", "eq"), rule.get("value", "")
    if f.get("_option"):
        on = op in ("checked", "not_empty") or (op == "eq" and str(want).lower() in ("true", "yes", "1", "on"))
        return [f["_option"]] if on else []
    if op in ("eq", "contains"):
        return str(want)
    if op in ("neq", "not_contains"):
        c = candidates(rule, f)
        return c[1] if len(c) > 1 else None
    if op in ("gt", "gte", "lt", "lte"):
        n = _num(want)
        if n is None:
            return None
        return _fmt({"gt": n + 1, "gte": n, "lt": n - 1, "lte": n}[op])
    if op in ("empty", "not_checked"):
        return [] if f.get("type") == "checkbox" else ""
    if op in ("not_empty", "checked"):
        return True if f.get("type") in ("clickwrap",) else baseline_value(f)
    if op in ("after", "until"):
        c = candidates(rule, f)
        return (c[1] if op == "after" else c[0]) if c else None
    return None


def satisfy(cond, idx):
    """{(doc_key, field_id): value} that makes ``cond`` true, ignoring @context rules; None if impossible."""
    rules = [r for r in (cond or {}).get("rules", []) if isinstance(r, dict) and (r.get("field") or "rules" in r)]
    if not rules:
        return {}
    parts = []
    for r in rules:
        if "rules" in r:
            parts.append(satisfy(r, idx))
        elif str(r["field"]).startswith("@") or str(r["field"]) in COMPUTED.get():
            parts.append({})  # decided by the run (context) or by other inputs (computed)
        else:
            hit = idx.get(str(r["field"]))
            v = truthy(r, hit[1]) if hit else None
            parts.append(None if v is None else {(hit[0], hit[1]["id"]): v})
    if cond.get("logic") == "or":
        return next((p for p in parts if p is not None), None)
    out = {}
    for p in parts:
        if p is None:
            return None
        for k, v in p.items():
            if k in out and out[k] != v:
                return None  # two rules want different values for the same field
            out[k] = v
    return out


def _describe(delta, idx_by_id):
    parts = []
    for (dk, fid), v in delta.items():
        f = idx_by_id.get((dk, fid), {})
        name = f.get("label") or f.get("name") or fid
        if isinstance(v, list) and f.get("type") == "checkbox":
            parts.append(f"{name}: {', '.join(map(str, v)) or 'nothing ticked'}")
        else:
            parts.append(f"{name} = {v}" if v not in ("", None) else f"{name} left empty")
    text = "; ".join(parts)
    return text if len(text) <= 160 else text[:157] + "…"


def scenarios(snapshot):
    """(base values, [(label, delta)], unknown references). Deltas: one per condition that can be met, then one per
    single value that flips a rule. The explorer combines them as it finds new parts of the workflow."""
    idx = _field_index(snapshot)
    COMPUTED.set(frozenset(_computed(snapshot)))
    base = {}
    for d in snapshot.get("documents", []):
        for f in d.get("fields", []):
            if is_input(f.get("type")) and f.get("type") not in ("formula",):
                base[(d["key"], f["id"])] = baseline_value(f)
    deltas, sigs, unknown = [], set(), []

    def add(label, delta):
        delta = {k: v for k, v in delta.items() if str(base.get(k)) != str(v)}
        sig = tuple(sorted((k, str(v)) for k, v in delta.items()))
        if delta and sig not in sigs:
            sigs.add(sig)
            deltas.append((label, delta))

    for where, cond in _conditions(snapshot):
        want = satisfy(cond, idx)
        if want:
            add(f"Meets “{where}”", want)
    computed = _computed(snapshot)
    for where, cond in _conditions(snapshot):  # computed values: push their inputs apart, both ways
        for r in _rules(cond):
            ref = str(r["field"])
            if ref not in computed:
                continue
            leaves = [idx[x] for x in sorted(_leaves(ref, computed)) if x in idx]
            for i, (dk, f) in enumerate(leaves):
                hi = {(lk, lf["id"]): "0" for lk, lf in leaves}
                hi[(dk, f["id"])] = "100000"
                lo = {(lk, lf["id"]): "100000" for lk, lf in leaves}
                lo[(dk, f["id"])] = "0"
                add(f"{f.get('label') or f.get('name')} high, other inputs low", hi)
                add(f"{f.get('label') or f.get('name')} low, other inputs high", lo)
    for where, cond in _conditions(snapshot):
        for r in _rules(cond):
            ref = str(r["field"])
            if ref.startswith("@") or ref in computed:
                continue  # context values are decided by the run; computed ones by their inputs (above)
            hit = idx.get(ref)
            if hit is None:
                unknown.append((where, ref))
                continue
            dk, f = hit
            for v in candidates(r, f):
                add("", {(dk, f["id"]): v})
    return base, deltas, unknown


def routing_prefill(snapshot):
    """Field ids a start form would supply: condition fields no step fills, or all of them when routing happens before
    anyone acts (the workflow opens with a branch or a conditional step)."""
    from . import diagram
    from .fieldtypes import field_roles

    steps = snapshot.get("steps", [])
    first = next((x for x in steps if x["key"] == diagram.first(steps)), None) if steps else None
    early = bool(first and (first.get("kind") == "branch" or first.get("condition")))
    recipients = {x["key"] for x in steps if x.get("kind") not in ("branch", "bot")}
    idx = _field_index(snapshot)
    out = set()
    for _, cond in _conditions(snapshot):
        for r in _rules(cond):
            hit = idx.get(str(r["field"]))
            if not hit:
                continue
            dk, f = hit
            roles = set(field_roles(f))
            fillable = not (f.get("readonly") or f.get("hidden")) and ("anyone" in roles or roles & recipients)
            if early or not fillable:
                out.add((dk, f["id"]))
    return out


# ---- running -----------------------------------------------------------------------------------------------------
def _intended(packet, step):
    """Who a real (non-test) run would send this step to."""
    from .engine import _resolve_assignees

    packet.is_test = False
    try:
        targets = _resolve_assignees(packet, step)
    finally:
        packet.is_test = True
    out = []
    for t in targets:
        g = t.get("group")
        if g is not None:
            members = g.members.count()
            out.append({"label": f"group {g.name}", "empty": members == 0})
        else:
            out.append({"label": t.get("email") or t.get("name") or "nobody", "empty": not (t.get("email") or t.get("user")
                                                                                         or t.get("name") == "Anyone with the link")})
    return out


def _values_for(packet, run, values):
    from .engine import editable_field_ids

    out = {}
    for dk, ids in editable_field_ids(packet, run.step_key).items():
        for fid in ids:
            if (dk, fid) in values:
                out.setdefault(dk, {})[fid] = values[(dk, fid)]
    return out


def run_scenario(workflow, user, values, base=None, prefill_keys=()):
    from . import engine
    from .models import Packet

    steps_def = {s["key"]: s for s in workflow.build_snapshot().get("steps", [])}
    path, problems, counts = [], [], {}
    outcome = "completed"
    with simulating() as log:
        try:
            prefill = {fid: values[(dk, fid)] for dk, fid in prefill_keys if (dk, fid) in values}
            packet = engine.start_packet(workflow, user=user, source="simulation", is_test=True, name=f"[SIM] {workflow.name}",
                                         prefill=prefill or None,
                                         first_assignee={"email": user.email, "name": engine.actor_label(user), "user": user})
            activations = 0
            while True:
                packet.refresh_from_db()
                if packet.status != Packet.IN_PROGRESS:
                    break
                runs = list(engine.open_runs(packet).order_by("pk"))
                if not runs:
                    outcome = "stuck"
                    problems.append("The document stopped with no open step and did not complete.")
                    break
                run = runs[0]
                activations += 1
                counts[run.step_key] = counts.get(run.step_key, 0) + 1
                step = steps_def.get(run.step_key) or {"name": run.step_name}
                who = _intended(packet, step) if run.step_key in steps_def else []
                path.append({"step": run.step_name, "key": run.step_key, "to": ", ".join(w["label"] for w in who) or "the sender"})
                for w in who:
                    if w["empty"]:
                        problems.append(f"“{run.step_name}” goes to {w['label']}, which has no one to receive it.")
                if counts[run.step_key] >= LOOP_LIMIT or activations >= MAX_ACTIVATIONS:
                    outcome = "loop"
                    problems.append(f"“{run.step_name}” opened {counts[run.step_key]} times: the route loops.")
                    break
                # first visit gives the scenario's answer; coming back (revise / more info), people give the baseline one
                vals = _values_for(packet, run, values if counts[run.step_key] == 1 or base is None else base)
                engine.complete_run(run, vals, user=user, label="Simulator")
        except Exception as exc:  # noqa: BLE001 - an engine error is a finding, not a crash
            outcome = "error"
            problems.append(f"{type(exc).__name__}: {str(exc)[:200]}")
            packet = None
        missing = []
        if packet is not None:
            packet.refresh_from_db()
            for d in packet.documents:
                vals = (packet.data or {}).get(d["key"], {})
                for f in d.get("fields", []):
                    if f.get("required") and is_input(f.get("type")) and vals.get(f["id"]) in (None, "", [], {}):
                        from .conditions import evaluate

                        if evaluate(f.get("condition"), packet.flat_values()):
                            missing.append(f.get("label") or f.get("name") or f["id"])
        if packet is not None:  # the real route, automated steps included
            from .models import StepRun

            path, intended = [], {}
            for sr in StepRun.objects.filter(packet=packet).order_by("pk"):
                if path and path[-1]["key"] == sr.step_key and path[-1]["run"] != sr.pk and sr.status == "skipped":
                    continue  # parallel recipients of the same step
                st = steps_def.get(sr.step_key) or {}
                if (st.get("settings") or {}).get("auto_complete"):
                    to = "automated"
                else:
                    if sr.step_key not in intended:
                        intended[sr.step_key] = ", ".join(w["label"] for w in _intended(packet, st)) if st else ""
                    to = intended[sr.step_key] or "the sender"
                path.append({"step": sr.step_name, "key": sr.step_key, "to": to, "run": sr.pk})
        skipped = []
        if packet is not None:
            from .models import AuditEvent

            for ev in AuditEvent.objects.filter(packet=packet, action__in=["branch.matched", "step.jump", "step.skipped"]).order_by("pk"):
                skipped.append(ev.detail)
    return {"outcome": outcome, "path": path, "decisions": skipped, "problems": problems,
            "required_empty": missing if outcome == "completed" else [],
            "emails": log["emails"], "bots": log["bots"]}


def _merge(a, b):
    out = dict(a)
    for k, v in b.items():
        if k in out and str(out[k]) != str(v):
            return None
        out[k] = v
    return out


def simulate(workflow, user, *, budget=MAX_SCENARIOS, seconds=120, progress=None):
    """Explore the workflow with rolled-back test runs until every step is reached or the budget runs out.
    Runs the baseline and each single delta first; any run that reaches new steps is combined with every other delta."""
    import time
    from collections import deque

    snap = workflow.build_snapshot()
    base, deltas, unknown = scenarios(snap)
    pre = routing_prefill(snap)
    by_id = {(d["key"], f["id"]): f for d in snap.get("documents", []) for f in d.get("fields", [])}
    recipient_keys = {x["key"] for x in snap.get("steps", []) if x.get("kind") not in ("branch", "bot")}
    queue = deque([("Baseline: every field filled with sample values", {}, True)] + [(lb, d, True) for lb, d in deltas])
    first_round = len(queue)  # each decision is tried at least once unless the budget runs out first
    tried, reached, results = set(), set(), []
    started, popped = time.monotonic(), 0
    first_left = first_round
    while queue and len(results) < budget and time.monotonic() - started < seconds \
            and not (recipient_keys <= reached and first_left == 0):
        popped += 1
        label, delta, first = queue.popleft()
        first_left -= first
        sig = tuple(sorted((k, str(v)) for k, v in delta.items()))
        if sig in tried:
            continue
        tried.add(sig)
        with transaction.atomic():
            r = run_scenario(workflow, user, {**base, **delta}, base=base, prefill_keys=pre)
            transaction.set_rollback(True)
        r["label"] = label or _describe(delta, by_id)
        results.append(r)
        new = {p["key"] for p in r["path"]} - reached
        if new:
            reached |= new
            r["new_steps"] = len(new)
            deeper = []
            for _, other in deltas:  # go deeper from here first: these runs already got further than the rest
                m = _merge(delta, other)
                if m is not None and m != delta:
                    deeper.append(("", m, False))
            queue.extendleft(reversed(deeper))
        if progress:
            progress(len(results), len(reached & recipient_keys), len(recipient_keys))
    snap_steps = snap.get("steps", [])
    recipient_steps = [s for s in snap_steps if s.get("kind") not in ("branch", "bot")]
    problems = []
    for where, ref in unknown:
        problems.append({"severity": "high", "text": f"{where} checks “{ref}”, which isn't a field in this workflow, so it never matches."})
    order = [x["key"] for x in snap_steps]
    names = {x["key"]: x["name"] for x in snap_steps}
    for x in snap_steps:  # a jump back to a step that already ran is ignored unless that step may run again
        for j in (x.get("settings") or {}).get("jumps") or []:
            t = next((y for y in snap_steps if y["key"] == j.get("to")), None)
            if t and t.get("kind") != "branch" and order.index(t["key"]) <= order.index(x["key"]) \
                    and not (t.get("settings") or {}).get("recurrence"):
                problems.append({"severity": "high", "text": f"The jump from “{x['name']}” back to “{names[t['key']]}” does nothing: "
                                                             f"“{names[t['key']]}” has already run and Recurrence is off in its settings."})
            elif t and t.get("kind") != "branch" and order.index(t["key"]) < order.index(x["key"]):
                for y in snap_steps[order.index(t["key"]) + 1:order.index(x["key"]) + 1]:
                    if y.get("kind") not in ("branch", "bot") and not (y.get("settings") or {}).get("recurrence"):
                        problems.append({"severity": "high", "text": f"After the jump back to “{names[t['key']]}”, “{y['name']}” is skipped the "
                                                                     f"second time round because Recurrence is off in its settings."})
    exhausted = bool(queue) and not recipient_keys <= reached
    for s in recipient_steps:
        if s["key"] not in reached:
            problems.append({"severity": "low", "text": f"“{s['name']}” wasn't reached in {len(results)} runs; it may need a combination "
                                                         f"the simulator didn't get to."} if exhausted else
                            {"severity": "medium", "text": f"“{s['name']}” is never reached: no answers lead to it."})
    seen = set()
    for r in results:
        for p in r["problems"]:
            if p not in seen:
                seen.add(p)
                problems.append({"severity": "high" if r["outcome"] in ("error", "stuck", "loop") else "medium", "text": p})
    # same path from every scenario while there are branches → the conditions may never differ
    if len({tuple(p["key"] for p in r["path"]) for r in results}) == 1 and any(s.get("kind") == "branch" for s in snap.get("steps", [])):
        problems.append({"severity": "medium", "text": "Every scenario took the same route, although the workflow has branches."})
    return {"scenarios": results, "problems": problems, "steps_total": len(recipient_steps),
            "steps_reached": len([s for s in recipient_steps if s["key"] in reached]),
            "complete": recipient_keys <= reached, "exhausted": exhausted, "runs": len(results),
            "ran_at": timezone.now().isoformat()}


# ---- background runs (the page polls status) --------------------------------------------------------------------
def _key(workflow_id):
    return f"simulation:{workflow_id}"


def status(workflow_id):
    from django.core.cache import cache

    return cache.get(_key(workflow_id)) or {"status": "idle"}


def start(workflow, user):
    """Run in a background thread; False if one is already running for this workflow."""
    import threading

    from django.core.cache import cache
    from django.db import close_old_connections

    if status(workflow.pk).get("status") == "running":
        return False
    cache.set(_key(workflow.pk), {"status": "running", "runs": 0, "reached": 0, "total": 0}, 3600)

    def progress(runs, reached, total):
        cache.set(_key(workflow.pk), {"status": "running", "runs": runs, "reached": reached, "total": total}, 3600)

    def work():
        try:
            result = simulate(workflow, user, progress=progress)
            cache.set(_key(workflow.pk), {"status": "done", "result": result, "by": user.pk}, 3600 * 24)
        except Exception as exc:  # noqa: BLE001
            cache.set(_key(workflow.pk), {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}, 3600)
        finally:
            close_old_connections()
    threading.Thread(target=work, daemon=True).start()
    return True


def routes(result):
    """Scenarios grouped by the route they took, most common first."""
    groups = {}
    for sc in result.get("scenarios", []):
        sig = (tuple(p["key"] for p in sc["path"]), sc["outcome"])
        g = groups.setdefault(sig, {"path": sc["path"], "outcome": sc["outcome"], "labels": [], "emails": 0, "bots": set(),
                                    "required_empty": set()})
        g["labels"].append(sc["label"])
        g["emails"] = max(g["emails"], len(sc["emails"]))
        g["bots"] |= {b["name"] for b in sc["bots"]}
        g["required_empty"] |= set(sc["required_empty"])
    out = list(groups.values())
    for g in out:
        g["bots"], g["required_empty"] = sorted(g["bots"]), sorted(g["required_empty"])
    return sorted(out, key=lambda g: (g["outcome"] != "completed", -len(g["labels"])))
