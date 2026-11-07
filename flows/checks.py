"""Workflow checks: everything the builder can find wrong with a workflow before it runs.

``run(wf)`` returns a list of findings::

    {"level": "error" | "warning" | "info", "code": "...", "message": "...", "step": <step key or "">,
     "fix": <a change request the AI assistant can act on, or "">}

Errors block Publish (they are what ``builder.validate`` used to return). Warnings are mistakes that will make
documents behave unexpectedly: links to steps that no longer exist, rules on deleted fields, lanes that can never
be taken. Info is advice. The same list feeds the builder's Checks panel, the canvas markers and the AI review.
"""
import re

from . import diagram
from .fieldtypes import field_roles

ERROR, WARNING, INFO = "error", "warning", "info"
LEVEL_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}
_TOKEN = re.compile(r"\{field:([^}]+)\}")


def _finding(level, code, message, step="", fix=""):
    return {"level": level, "code": code, "message": message, "step": step, "fix": fix}


class _Ctx:
    def __init__(self, wf):
        from .models import Step

        self.wf = wf
        self.Step = Step
        self.steps = wf.ordered_steps()
        self.docs = list(wf.documents.all())
        self.bots = list(wf.bots.all())
        self.by_key = {s.key: s for s in self.steps}
        self.aliases = {a: s for s in self.steps for a in (s.settings or {}).get("aliases") or []}
        self.fields = [(d, f) for d in self.docs for f in d.input_fields()]
        from .models import option_keys

        # the same names Packet.flat_values() exposes to conditions: id, name, label and checkbox "<name>-<option>"
        self.field_refs = set()
        for _d, f in self.fields:
            for ref in (f.get("id"), f.get("name"), f.get("label"), *option_keys(f, [])):
                if ref:
                    self.field_refs.add(str(ref).strip())
        self.doc_keys = {d.key for d in self.docs}

    def step_name(self, key):
        s = self.by_key.get(key) or self.aliases.get(key)
        return s.name if s else key

    def knows_step(self, key):
        return key in self.by_key or key in self.aliases

    def knows_field(self, ref):
        ref = str(ref or "").strip()
        if not ref:
            return True
        if ref.startswith("@"):
            if ref.startswith(("@any:", "@all:")):
                return all(self.knows_field(x) for x in ref[5:].split(",") if x.strip())
            m = re.match(r"@(filled|all_fillable|all_required):(.+)", ref)
            if m:
                return m.group(2) in self.doc_keys
            return True  # @step, @status, @document and other built-in keys
        return ref in self.field_refs  # conditions match names exactly, so "amount" is not "Amount"


def _rule_fields(condition):
    """Every field a condition (with nested groups) reads."""
    out = []

    def visit(c):
        for r in (c or {}).get("rules") or []:
            if "rules" in r:
                visit(r)
            elif r.get("field"):
                out.append(r["field"])
            ref = _TOKEN.fullmatch(str(r.get("value") or "").strip())
            if ref:
                out.append(ref.group(1))
    visit(condition)
    return out


def _unknown_fields(ctx, condition):
    return sorted({str(f) for f in _rule_fields(condition) if not ctx.knows_field(f)})


def _tokens(value):
    """{field:X} tokens anywhere inside a (nested) config value."""
    if isinstance(value, str):
        return _TOKEN.findall(value)
    if isinstance(value, dict):
        return [t for v in value.values() for t in _tokens(v)]
    if isinstance(value, list):
        return [t for v in value for t in _tokens(v)]
    return []


def _structure(ctx, out):
    """The checks Publish has always enforced."""
    S = ctx.Step
    recips = [s for s in ctx.steps if s.kind == S.RECIPIENT]
    if not recips:
        out.append(_finding(ERROR, "no_recipient_step", "Add at least one recipient step.",
                            fix="Add a first step where the requester fills out the form."))
    if not ctx.docs:
        out.append(_finding(ERROR, "no_document", "Add at least one document or form."))
    for s in recips:
        if s.recipient_type == S.R_CONTACT and not s.recipient_contact_id:
            out.append(_finding(ERROR, "no_contact", f"Choose a contact for “{s.name}”.", s.key))
        if s.recipient_type == S.R_GROUP and not s.recipient_group_id:
            out.append(_finding(ERROR, "no_group", f"Choose a group for “{s.name}”.", s.key))
        if s.recipient_type == S.R_ANYONE and s != recips[0]:
            out.append(_finding(ERROR, "anyone_not_first",
                                f"“{s.name}”: only the first step can be filled by anyone with the link.", s.key))


def _recipients(ctx, out):
    S = ctx.Step
    for s in ctx.steps:
        if s.kind != S.RECIPIENT:
            continue
        st = s.settings or {}
        if (s.recipient_type == S.R_EMAIL and not (s.recipient_email or "").strip() and not st.get("auto_complete")
                and ctx.wf.routing_mode != "bots"):
            out.append(_finding(WARNING, "no_email", f"“{s.name}” has no recipient email, so nobody will be invited.",
                                s.key, fix=f"Set the recipient of the “{s.name}” step."))
        sla = st.get("sla") or {}
        if sla and sla.get("escalate") == "reassign" and not (sla.get("escalate_to") or "").strip():
            out.append(_finding(WARNING, "sla_no_target",
                                f"“{s.name}”: the deadline reassigns the step but says to no one.", s.key))
        if s.recipient_group_id and s.recipient_group and not s.recipient_group.members.exists():
            out.append(_finding(WARNING, "empty_group",
                                f"“{s.name}” is assigned to the group “{s.recipient_group.name}”, which has no members.",
                                s.key))


def _links(ctx, out):
    """Jumps, branch targets and conditions that point at steps or fields that no longer exist."""
    S = ctx.Step
    for s in ctx.steps:
        for j in (s.settings or {}).get("jumps") or []:
            if j.get("to") and not ctx.knows_step(j["to"]):
                out.append(_finding(WARNING, "jump_missing", f"“{s.name}” jumps to a step that was deleted.", s.key,
                                    fix=f"Remove the broken jump from the “{s.name}” step."))
        bad = _unknown_fields(ctx, s.condition)
        if bad:
            out.append(_finding(WARNING, "condition_field_missing",
                                f"“{s.name}” only runs when {', '.join(bad)} matches, but that field no longer exists.",
                                s.key))
        for j in (s.settings or {}).get("jumps") or []:
            bad = _unknown_fields(ctx, {"rules": j.get("rules") or []})
            if bad:
                out.append(_finding(WARNING, "jump_field_missing",
                                    f"A jump on “{s.name}” reads {', '.join(bad)}, which no longer exists.", s.key))
        if s.kind != S.BRANCH:
            continue
        lanes = s.branches or []
        if not lanes:
            out.append(_finding(WARNING, "branch_empty", f"“{s.name}” has no branches.", s.key))
        for i, ln in enumerate(lanes):
            label = ln.get("label") or f"Branch {i + 1}"
            for attr in ("target", "then"):
                tgt = ln.get(attr) or ""
                if tgt and tgt not in ("lane", "next", "merge", "end") and not ctx.knows_step(tgt):
                    out.append(_finding(WARNING, "branch_target_missing",
                                        f"“{s.name}” › {label} goes to a step that was deleted.", s.key))
            bad = _unknown_fields(ctx, {"rules": ln.get("rules") or []})
            if bad:
                out.append(_finding(WARNING, "branch_field_missing",
                                    f"“{s.name}” › {label} reads {', '.join(bad)}, which no longer exists.", s.key))
            if not _rule_fields({"rules": ln.get("rules") or []}) and i < len(lanes) - 1:
                later = ", ".join(f"“{x.get('label') or 'Branch'}”" for x in lanes[i + 1:])
                out.append(_finding(WARNING, "lane_shadows",
                                    f"“{s.name}” › {label} has no rules, so it is always taken and {later} never run.",
                                    s.key, fix=f"Give the “{label}” branch of “{s.name}” a rule, or move it last."))
            if (ln.get("target") or "lane") == "lane" and not diagram.children(ctx.steps, (s.key, ln.get("id") or f"i{i}")):
                out.append(_finding(INFO, "lane_no_steps", f"“{s.name}” › {label} has no steps yet.", s.key))
    for d in ctx.docs:
        bad = _unknown_fields(ctx, d.condition)
        if bad:
            out.append(_finding(WARNING, "doc_condition_field_missing",
                                f"The document “{d.name}” is shown only when {', '.join(bad)} matches, "
                                "but that field no longer exists."))


def _bots(ctx, out):
    from .models import Bot

    for b in ctx.bots:
        if not b.enabled:
            continue
        if b.step_key and not ctx.knows_step(b.step_key):
            out.append(_finding(WARNING, "bot_step_missing", f"The bot “{b.name}” belongs to a step that was deleted.",
                                fix=f"Remove the bot “{b.name}”."))
        target = (b.config or {}).get("step")
        if b.bot_type == "route_to_step" and target and not ctx.knows_step(target):
            out.append(_finding(WARNING, "bot_route_missing", f"The bot “{b.name}” routes to a step that was deleted.",
                                b.step_key if ctx.knows_step(b.step_key) else ""))
        bad = _unknown_fields(ctx, b.condition)
        if bad:
            out.append(_finding(WARNING, "bot_field_missing",
                                f"The bot “{b.name}” runs when {', '.join(bad)} matches, but that field no longer exists.",
                                b.step_key if ctx.knows_step(b.step_key) else ""))
        missing = sorted({t for t in _tokens(b.config) if not ctx.knows_field(t)})
        if missing:
            out.append(_finding(WARNING, "bot_token_missing",
                                f"The bot “{b.name}” uses {', '.join('{field:' + t + '}' for t in missing)}, "
                                "but no field has that name.", b.step_key if ctx.knows_step(b.step_key) else "",
                                fix=f"Fix the field references in the bot “{b.name}”."))
    for s in ctx.steps:
        if s.kind == ctx.Step.BOT and not any(b.trigger == Bot.INLINE and b.step_key == s.key for b in ctx.bots):
            out.append(_finding(WARNING, "bot_step_empty", f"The bot step “{s.name}” has no bot configured.", s.key))


def _fields(ctx, out):
    S = ctx.Step
    orphans = []
    for d, f in ctx.fields:
        roles = [r for r in field_roles(f) if r and r != "anyone"]
        if roles and not any(ctx.knows_step(r) for r in roles):
            orphans.append((d, f))
    for d, f in orphans[:8]:
        label = f.get("label") or f.get("name") or f["id"]
        level = WARNING if f.get("required") else INFO
        out.append(_finding(level, "field_no_step",
                            f"“{label}” in {d.name} is assigned to a step that was deleted, so nobody fills it"
                            + (" — and it is required." if f.get("required") else "."),
                            fix=f"Assign the field “{label}” to the step that should fill it."))
    if len(orphans) > 8:
        out.append(_finding(INFO, "field_no_step_more", f"{len(orphans) - 8} more fields are assigned to deleted steps."))
    for s in ctx.steps:
        if s.kind != S.RECIPIENT or (s.settings or {}).get("auto_complete"):
            continue
        fills = [d for d in ctx.docs if (s.document_access or {}).get(d.key, "fill") == "fill"]
        if ctx.docs and not fills and not any((s.document_access or {}).get(d.key) == "view" for d in ctx.docs):
            out.append(_finding(WARNING, "step_sees_nothing", f"“{s.name}” can't see any document.", s.key))
    names = {}
    for s in ctx.steps:
        if s.kind != S.RECIPIENT:  # branch and bot steps share default names harmlessly
            continue
        names.setdefault(s.name.strip().lower(), []).append(s)
    for same in names.values():
        if len(same) > 1:
            out.append(_finding(INFO, "duplicate_step_name",
                                f"{len(same)} steps are called “{same[0].name}”; rules and reports can't tell them apart.",
                                same[1].key))


def run(wf):
    ctx = _Ctx(wf)
    out = []
    for check in (_structure, _recipients, _links, _bots, _fields):
        check(ctx, out)
    out.sort(key=lambda f: LEVEL_ORDER[f["level"]])
    return out


def errors(findings):
    return [f["message"] for f in findings if f["level"] == ERROR]


def summary(findings):
    counts = {ERROR: 0, WARNING: 0, INFO: 0}
    for f in findings:
        counts[f["level"]] += 1
    return counts


def by_step(findings):
    out = {}
    for f in findings:
        if f["step"] and f["level"] != INFO:
            out.setdefault(f["step"], []).append(f)
    return out


def as_text(findings, limit=12):
    """Plain lines for a model prompt (no keys or ids)."""
    return "\n".join(f"- [{f['level']}] {f['message']}" for f in findings[:limit]) or "- none"
