"""Workflow blueprints: build a complete workflow (documents, steps, branch lanes, jumps, bots, autostarts and an
optional start form) from one JSON description, and export any workflow back to that format.

Used by the MCP server (``manage.py paperlane_mcp``) so AI assistants can build and inspect workflows.

Blueprint (all keys optional unless noted)::

    {
      "name": "Purchase approval",                      # required
      "description": "...",
      "documents": [                                     # at least one
        {"key": "request", "name": "Purchase request",
         "fields": [{"id": "item", "type": "text", "label": "Item", "required": true,
                     "filled_by": ["requester"],          # step keys (or "anyone")
                     "options": [...], "condition": {...}, "formula": "...", "width": "half", ...}]}],
      "steps": [                                         # in order; lanes nest inside branches
        {"key": "requester", "name": "Requester", "recipient": {"type": "anyone"},
         "documents": {"request": "fill"}},              # fill | view | hidden (default fill)
        {"key": "route", "type": "branch", "name": "Route by amount",
         "lanes": [{"id": "big", "label": "Over 5,000", "when": {...}, "then": "merge",
                    "steps": [{"key": "finance", "name": "Finance", "recipient": {"type": "email", "email": "cfo@x.com"}}]},
                   {"id": "small", "label": "Up to 5,000", "when": {...}, "target": "next"}]},
        {"key": "manager", "name": "Manager", "recipient": {"type": "group", "group": "Managers"},
         "condition": {...}, "jumps": [{"to": "requester", "when": {...}}],
         "settings": {"recurrence": true, "reminder": {"enabled": true, "every_days": 1, "max": 3}}}],
      "bots": [{"type": "add_tags", "name": "Tag", "trigger": "flow_complete", "step": "manager",
                "config": {"tags": "#Done"}, "when": {...}}],
      "autostart": [{"type": "schedule", ...} | {"type": "webhook", "source": "..."}],
      "start_form": {"name": "...", "fields": [...], "lookups": [...], "options": {...}, "publish": true}
    }

Conditions: {"logic": "and"|"or", "rules": [{"field": "<field name or label>", "op": "eq", "value": "x"}, {nested}]}.
Recipient types: email (also "{field:Email}"), group (by name; created if missing), anyone, current_user, contact (by email).
Lane "target": lane (run the lane's steps, default when it has steps) | next | end | <step key>;
lane "then" (after its steps): merge | end | <step key>.
"""
import secrets

from django.db import transaction

from .bots import CATALOG
from .fieldtypes import CONDITION_OPERATORS, CONDITION_TYPES, ELEMENTS, WIDGETS
from .models import Bot, Step, Workflow, WorkflowDocument

FIELD_TYPES = [t for _, items in ELEMENTS for t, _, _ in items] + [t for _, items in WIDGETS for t, _, _ in items]
TRIGGERS = [k for k, _ in Bot.TRIGGERS]


class BlueprintError(ValueError):
    pass


def guide():
    """A compact reference an AI assistant can read before writing a blueprint."""
    return {
        "blueprint_format": __doc__.split("Blueprint (all keys optional unless noted)::", 1)[1].strip(),
        "field_types": FIELD_TYPES,
        "condition_operators": [k for k, _ in CONDITION_OPERATORS],
        "special_condition_fields": {g: [k for k, _ in items] for g, items in CONDITION_TYPES}
        | {"When the number of filled fields in": ["@filled:<document key>"],
           "All fields of a document": ["@all_fillable:<document key>", "@all_required:<document key>"],
           "Several fields at once": ["@any:FieldA,FieldB (any matches)", "@all:FieldA,FieldB (all match)"]},
        "bot_triggers": TRIGGERS,
        "bot_types": {k: {"name": m["name"], "category": m["category"],
                          "config": [{"name": f["name"], "type": f["type"], "required": f.get("required", False)} for f in m["fields"]]}
                      for k, m in CATALOG.items() if not m.get("alias")},
        "recipient_types": ["email", "group", "anyone", "current_user", "contact"],
        "document_access": ["fill", "view", "hidden"],
        "autostart_types": ["schedule", "sheet (needs an uploaded file — use the UI)", "workflow", "webhook"],
        "tips": [
            "Field ids must be unique within a document; give fields a 'name' — conditions, bots and pre-fill use it.",
            "Every field needs filled_by (step keys) or it can be filled by anyone.",
            "Only the first step may use recipient type 'anyone' (public link / start form).",
            "A start form maps to the workflow by field name, so reuse the workflow field names in the form.",
            "Use publish: true to make the workflow runnable; check the returned 'problems' list.",
            "Deadlines: step settings.sla = {amount, unit: hours|days|business_days, escalate: notify|reassign|none, "
            "escalate_to: email or group name, tag}. Overdue steps are flagged, the holder and owner are told, and "
            "escalation notifies or reassigns.",
        ],
    }


def _cond(c, *, field_ids=None, step_keys=None):
    """Normalise a condition and translate step keys (@step) if given."""
    if not c:
        return {}
    if isinstance(c, list):
        c = {"logic": "and", "rules": c}
    out = {"logic": c.get("logic", "and"), "rules": []}
    for r in c.get("rules") or []:
        if isinstance(r, dict) and "rules" in r:
            out["rules"].append(_cond(r, field_ids=field_ids, step_keys=step_keys))
        elif isinstance(r, dict) and r.get("field"):
            rule = {"field": r["field"], "op": r.get("op", "eq"), "value": "" if r.get("value") is None else str(r.get("value"))}
            if step_keys and rule["field"] == "@step" and rule["value"] in step_keys:
                rule["value"] = step_keys[rule["value"]]
            if field_ids and rule["field"] in field_ids:  # form-field conditions may use the field id
                rule["field"] = field_ids[rule["field"]]
            out["rules"].append(rule)
    return out


def _flatten_steps(steps, lane=None, out=None):
    """Depth-first list of (spec, lane) pairs; branch lanes' steps follow their branch."""
    out = [] if out is None else out
    for s in steps or []:
        if not isinstance(s, dict) or not s.get("key"):
            raise BlueprintError("Every step needs a unique 'key'.")
        out.append((s, lane))
        if (s.get("type") or "step") == "branch":
            for i, ln in enumerate(s.get("lanes") or []):
                lid = str(ln.get("id") or f"lane{i + 1}")
                _flatten_steps(ln.get("steps"), (s["key"], lid), out)
    return out


@transaction.atomic
def build_workflow(workspace, user, spec, *, publish=False, strict=True):
    """Create a workflow from a blueprint. Returns (workflow, problems list).

    strict=False (imports) drops references to steps that no longer exist instead of failing."""
    from accounts.models import Contact, Group

    from . import autostart as auto_mod
    from . import services

    if not isinstance(spec, dict) or not str(spec.get("name", "")).strip():
        raise BlueprintError("The blueprint needs a 'name'.")
    docs_spec = spec.get("documents") or []
    if not docs_spec:
        raise BlueprintError("Add at least one document with fields.")
    flat = _flatten_steps(spec.get("steps") or [])
    if not any((s.get("type") or "step") == "step" for s, _ in flat):
        raise BlueprintError("Add at least one recipient step.")
    seen = set()
    for s, _ in flat:
        if s["key"] in seen:
            raise BlueprintError(f"Step key '{s['key']}' is used twice.")
        seen.add(s["key"])

    wf = Workflow.objects.create(workspace=workspace, name=str(spec["name"]).strip()[:200], kind="workflow",
                                 description=str(spec.get("description", ""))[:500], created_by=user)
    keys = {}  # blueprint step key → Step.key
    objs = {}
    for order, (s, lane) in enumerate(flat, start=1):
        kind = {"step": Step.RECIPIENT, "branch": Step.BRANCH, "bot": Step.BOT}.get(s.get("type") or "step")
        if kind is None:
            raise BlueprintError(f"Step '{s['key']}': type must be step, branch or bot.")
        st = Step.objects.create(workflow=wf, order=order * 10, kind=kind, name=str(s.get("name") or s["key"])[:120])
        keys[s["key"]] = st.key
        objs[s["key"]] = (st, s, lane)

    doc_keys, doc_objs = {}, {}
    for i, d in enumerate(docs_spec):
        if not d.get("fields"):
            raise BlueprintError(f"Document '{d.get('name', i)}' has no fields.")
        fields, ids = [], set()
        for f in d["fields"]:
            if not f.get("id") or not f.get("type"):
                raise BlueprintError(f"Every field needs an 'id' and a 'type' (document '{d.get('name')}').")
            if f["type"] not in FIELD_TYPES:
                raise BlueprintError(f"Unknown field type '{f['type']}'. Use one of: {', '.join(FIELD_TYPES)}")
            if f["id"] in ids:
                raise BlueprintError(f"Field id '{f['id']}' is used twice in '{d.get('name')}'.")
            ids.add(f["id"])
            fld = {k: v for k, v in f.items() if k not in ("filled_by",)}
            fld.setdefault("label", f["id"])
            fld.setdefault("name", fld["label"])
            by = f.get("filled_by", f.get("role", "anyone"))
            if by in (None, "", "anyone"):
                fld["role"] = "anyone"
            else:
                by = by if isinstance(by, list) else [by]
                missing = [b for b in by if b not in keys]
                if missing and not strict:
                    by = [b for b in by if b in keys]
                    missing = []
                if missing:
                    raise BlueprintError(f"Field '{f['id']}' is filled by unknown step(s): {', '.join(missing)}")
                fld["role"] = [keys[b] for b in by] or "anyone"
            if fld.get("condition"):
                fld["condition"] = _cond(fld["condition"])
            fields.append(fld)
        doc = WorkflowDocument.objects.create(workflow=wf, name=str(d.get("name") or f"Document {i + 1}")[:200], order=i,
                                              schema={"fields": fields})
        doc_keys[str(d.get("key") or doc.key)] = doc.key
        doc_objs[doc.key] = doc

    for bk, (st, s, lane) in objs.items():
        st.condition = _cond(s.get("condition"), step_keys=keys)
        settings_ = dict(s.get("settings") or {})
        if lane:
            settings_["lane"] = {"branch": keys[lane[0]], "lane": lane[1]}
        if s.get("jumps"):
            settings_["jumps"] = []
            for j in s["jumps"]:
                if j.get("to") not in keys:
                    raise BlueprintError(f"Step '{bk}' jumps to unknown step '{j.get('to')}'.")
                c = _cond(j.get("when"), step_keys=keys)
                settings_["jumps"].append({"to": keys[j["to"]], "logic": c.get("logic", "and"), "rules": c.get("rules", [])})
        if st.kind == Step.BRANCH:
            lanes = []
            for i, ln in enumerate(s.get("lanes") or []):
                c = _cond(ln.get("when"), step_keys=keys)
                target = ln.get("target") or ("lane" if ln.get("steps") else "next")
                then = ln.get("then") or "merge"
                for name, val in (("target", target), ("then", then)):
                    if val not in ("lane", "next", "merge", "end") and val not in keys:
                        raise BlueprintError(f"Branch '{bk}' lane '{ln.get('label')}': unknown {name} '{val}'.")
                lanes.append({"id": str(ln.get("id") or f"lane{i + 1}"), "label": str(ln.get("label") or f"Branch {i + 1}"),
                              "logic": c.get("logic", "and"), "rules": c.get("rules", []),
                              "target": keys.get(target, target), "then": keys.get(then, then)})
            st.branches = lanes
        if st.kind == Step.RECIPIENT:
            r = s.get("recipient") or {"type": "email"}
            rtype = r.get("type", "email")
            if rtype == "group":
                g, _ = Group.objects.get_or_create(workspace=workspace, name=str(r.get("group") or "Approvers")[:120])
                emails = r.get("members") or []
                if emails:
                    from accounts.models import Membership

                    g.members.add(*Membership.objects.filter(workspace=workspace, user__email__in=emails))
                st.recipient_type, st.recipient_group = Step.R_GROUP, g
            elif rtype == "contact":
                c = Contact.objects.filter(workspace=workspace, email__iexact=r.get("email", "")).first()
                if c is None and r.get("email"):
                    c = Contact.objects.create(workspace=workspace, email=r["email"], name=r.get("name") or r["email"])
                st.recipient_type, st.recipient_contact = Step.R_CONTACT, c
            elif rtype in ("anyone", "current_user"):
                st.recipient_type = {"anyone": Step.R_ANYONE, "current_user": Step.R_CURRENT}[rtype]
            else:
                st.recipient_type, st.recipient_email = Step.R_EMAIL, str(r.get("email", ""))
                st.recipient_name = str(r.get("name", ""))
            if s.get("subject"):
                st.message_subject = str(s["subject"])[:200]
            if s.get("message"):
                st.message_body = str(s["message"])
            access = s.get("documents") or {}
            st.document_access = {dk: (access.get(bkey) or access.get(dk) or "fill") for bkey, dk in doc_keys.items()}
            if s.get("thank_you"):
                st.thank_you = s["thank_you"]
            for k in ("attach_documents", "revoke_on_complete"):
                if k in s:
                    setattr(st, k, bool(s[k]))
        st.settings = settings_
        st.save()

    for i, b in enumerate(spec.get("bots") or []):
        btype = b.get("type")
        if btype not in CATALOG:
            raise BlueprintError(f"Unknown bot type '{btype}'. Call get_build_guide for the list.")
        trigger = b.get("trigger") or "step_complete"
        if trigger not in TRIGGERS:
            raise BlueprintError(f"Bot '{btype}': trigger must be one of {', '.join(TRIGGERS)}.")
        if b.get("step") and b["step"] not in keys:
            raise BlueprintError(f"Bot '{btype}' refers to unknown step '{b['step']}'.")
        Bot.objects.create(workflow=wf, bot_type=btype, name=str(b.get("name") or CATALOG[btype]["name"])[:160],
                           trigger=trigger, step_key=keys.get(b.get("step"), ""), order=i, enabled=b.get("enabled", True),
                           config={**(b.get("config") or {}), "skip_on_fail": b.get("skip_on_fail", True)},
                           condition=_cond(b.get("when"), step_keys=keys), run_once=bool(b.get("run_once")))

    if spec.get("autostart"):
        items = []
        for a in spec["autostart"]:
            if a.get("type") == "sheet" and not strict:
                continue  # needs its uploaded file — re-add it in the builder
            item = {"enabled": True, **a, "id": auto_mod.new_id(), "count": 0}
            if item.get("type") == "workflow" and isinstance(item.get("source_workflow"), str):
                src = Workflow.objects.filter(workspace=workspace, name=item["source_workflow"]).first()
                item["source_workflow"] = src.pk if src else None
            items.append(item)
        auto_mod.save_items(wf, items)

    from .views.builder import validate

    problems = validate(wf)
    if publish and not problems:
        services.publish(wf, user)
    if spec.get("start_form"):
        build_form(workspace, user, {**spec["start_form"], "workflow_id": wf.pk})
    wf.refresh_from_db()
    return wf, problems


def build_form(workspace, user, spec):
    """Create a start form. spec: name, fields, lookups, options, workflow_id/workflow (name), template, publish."""
    import copy

    from .form_templates import TEMPLATES
    from .models import IntakeForm

    tpl = TEMPLATES.get(spec.get("template") or "")
    fields = spec.get("fields") or (copy.deepcopy(tpl["fields"]) if tpl else None)
    if not fields:
        raise BlueprintError("A form needs 'fields' (or a 'template').")
    ids = set()
    for f in fields:
        if not f.get("id") or f.get("type") not in FIELD_TYPES or f["id"] in ids:
            raise BlueprintError(f"Form field needs a unique 'id' and a valid 'type' (got {f.get('id')!r}/{f.get('type')!r}).")
        ids.add(f["id"])
        f["role"] = "anyone"
        f.setdefault("label", f["id"])
        f.setdefault("name", f["label"])
        if f.get("condition"):
            f["condition"] = _cond(f["condition"])
    wf = None
    if spec.get("workflow_id"):
        wf = Workflow.objects.filter(workspace=workspace, pk=spec["workflow_id"]).first()
    elif spec.get("workflow"):
        wf = Workflow.objects.live().filter(workspace=workspace, name=spec["workflow"]).first()
    lookups = spec.get("lookups") if spec.get("lookups") is not None else (copy.deepcopy(tpl["lookups"]) if tpl else [])
    for lk in lookups:
        lk.setdefault("id", secrets.token_hex(4))
        if lk.get("trigger") not in ids:
            raise BlueprintError(f"In-form bot '{lk.get('name')}' triggers on unknown field '{lk.get('trigger')}'.")
        from .form_templates import ensure_lookup_sheets

        ensure_lookup_sheets(workspace, [lk])
    form = IntakeForm.objects.create(
        workspace=workspace, workflow=wf, created_by=user, name=str(spec.get("name") or (tpl["name"] if tpl else "Form"))[:200],
        description=str(spec.get("description", ""))[:400], schema={"fields": fields}, design=spec.get("design") or {},
        options={**(copy.deepcopy(tpl["options"]) if tpl else {"submit_label": "Submit"}), **(spec.get("options") or {})},
        lookups=lookups, template_key=spec.get("template") or "",
        status=IntakeForm.PUBLISHED if spec.get("publish") else IntakeForm.DRAFT)
    return form


def export_workflow(wf):
    """The blueprint of an existing workflow (step keys are the workflow's own keys)."""
    from . import autostart as auto_mod
    from . import diagram

    steps = wf.ordered_steps()
    by_key = {s.key: s for s in steps}

    def step_spec(st):
        d = {"key": st.key, "name": st.name, "type": {"recipient": "step", "branch": "branch", "bot": "bot"}[st.kind]}
        if st.condition and st.condition.get("rules"):
            d["condition"] = st.condition
        s = {k: v for k, v in (st.settings or {}).items() if k not in ("lane", "jumps")}
        if s:
            d["settings"] = s
        if (st.settings or {}).get("jumps"):
            d["jumps"] = [{"to": j["to"], "when": {"logic": j.get("logic", "and"), "rules": j.get("rules", [])}}
                          for j in st.settings["jumps"]]
        if st.kind == Step.RECIPIENT:
            rt = st.recipient_type
            d["recipient"] = {"type": {"email": "email", "group": "group", "anyone": "anyone", "current_user": "current_user",
                                       "contact": "contact"}.get(rt, rt)}
            if rt == Step.R_EMAIL:
                d["recipient"]["email"] = st.recipient_email
            if rt == Step.R_GROUP and st.recipient_group:
                d["recipient"]["group"] = st.recipient_group.name
            if rt == Step.R_CONTACT and st.recipient_contact:
                d["recipient"]["email"] = st.recipient_contact.email
            d["documents"] = st.document_access or {}
            d["subject"], d["message"] = st.message_subject, st.message_body
        if st.kind == Step.BRANCH:
            d["lanes"] = []
            for i, b in enumerate(diagram.ensure_lane_ids([dict(x) for x in st.branches or []])):
                ln = {"id": b["id"], "label": b.get("label"), "when": {"logic": b.get("logic", "and"), "rules": b.get("rules", [])},
                      "target": b.get("target") or "next", "then": b.get("then") or "merge"}
                kids = diagram.children(steps, (st.key, b["id"]))
                if kids:
                    ln["steps"] = [step_spec(k) for k in kids]
                d["lanes"].append(ln)
        return d

    docs = []
    for doc in wf.documents.all():
        fields = []
        for f in doc.fields:
            f2 = dict(f)
            role = f2.pop("role", "anyone")
            f2["filled_by"] = role if role in ("anyone", None) else (role if isinstance(role, list) else [role])
            fields.append(f2)
        docs.append({"key": doc.key, "name": doc.name, "fields": fields})
    return {
        "id": wf.pk, "name": wf.name, "description": wf.description, "status": wf.status,
        "documents": docs, "steps": [step_spec(s) for s in diagram.children(steps, None)],
        "bots": [{"type": b.bot_type, "name": b.name, "trigger": b.trigger, "step": b.step_key or None, "config": b.config,
                  "when": b.condition or {}, "enabled": b.enabled} for b in wf.bots.all()],
        "autostart": auto_mod.items(wf),
        "start_forms": [{"id": f.pk, "name": f.name, "status": f.status} for f in wf.intake_forms.filter(archived_at__isnull=True)],
    }
