"""Step templates: save a recipient step (settings, the fields it fills, its bots) and insert it into any workflow.

Inserting gives the step, its fields and bots fresh keys. A field whose name is already taken in the target document
is renamed "Name (2)", and every rule and {field:…} token in the copied step and bots follows the new name, so the
copy works on its own.
"""
import copy
import json
import re

from django.db import transaction

from .models import Bot, Step, StepTemplate, WorkflowDocument, new_key

STEP_KEEP = ("name", "recipient_type", "recipient_email", "recipient_name", "message_subject", "message_body",
             "attach_documents", "revoke_on_complete", "thank_you", "condition")
SETTINGS_SKIP = ("lane", "jumps", "aliases", "source_id")  # these point at other steps of the old workflow


def capture(step):
    """The template data for ``step`` (a recipient step)."""
    wf = step.workflow
    fields = []
    for d in wf.documents.order_by("order", "id"):
        for f in d.fields:
            role = f.get("role")
            if role == step.key or (isinstance(role, list) and step.key in role):
                fields.append({k: v for k, v in f.items() if k not in ("x", "y", "page")})
    bots = [{**b.as_dict(), "enabled": b.enabled} for b in wf.bots.filter(step_key=step.key).exclude(trigger=Bot.INLINE)]
    return {
        "step": {k: getattr(step, k) for k in STEP_KEEP},
        "settings": {k: v for k, v in (step.settings or {}).items() if k not in SETTINGS_SKIP},
        "group": step.recipient_group_id, "contact": step.recipient_contact_id,
        "fields": fields, "bots": bots, "source_key": step.key,
    }


def save(step, user, name="", description=""):
    data = capture(step)
    return StepTemplate.objects.create(workspace=step.workflow.workspace, name=(name or step.name)[:120],
                                       description=description[:255], data=data, created_by=user)


def _rename(obj, names):
    """Follow renamed fields in rules ("field": "Old") and tokens ({field:Old})."""
    if not names:
        return obj
    raw = json.dumps(obj)
    for old, new in names.items():
        raw = raw.replace(json.dumps(old)[1:-1].join(['"field": "', '"']), json.dumps(new)[1:-1].join(['"field": "', '"']))
        raw = raw.replace("{field:" + json.dumps(old)[1:-1] + "}", "{field:" + json.dumps(new)[1:-1] + "}")
    return json.loads(raw)


@transaction.atomic
def insert(tpl, wf, *, after=0, lane=None):
    """Add the template's step to ``wf`` after the step with order ``after``. Returns the new Step."""
    from accounts.models import Contact, Group

    data = copy.deepcopy(tpl.data)
    old_key, key = data.get("source_key") or "", new_key()
    doc = wf.documents.filter(kind=WorkflowDocument.FORM).order_by("order", "id").first()
    names, fields_out = {}, []
    if data.get("fields"):
        if doc is None:
            doc = WorkflowDocument.objects.create(workflow=wf, name=f"{tpl.name} form", schema={"fields": []},
                                                  order=wf.documents.count())
        taken = {str(f.get("name") or f.get("label") or "").lower() for f in doc.fields}
        for f in data["fields"]:
            f = dict(f, id=new_key(), role=key)
            base = str(f.get("name") or f.get("label") or f["id"])
            name, n = base, 2
            while name.lower() in taken:
                name, n = f"{base} ({n})", n + 1
            if name != base:
                names[base] = name
                f["name"] = name
                if f.get("label") == base:
                    f["label"] = name
            taken.add(name.lower())
            fields_out.append(f)
        doc.schema = {**(doc.schema or {}), "fields": [*doc.fields, *fields_out]}
        doc.save(update_fields=["schema"])
    st = _rename(data["step"], names)
    settings = _rename(data.get("settings") or {}, names)
    if lane:
        settings["lane"] = lane
    group = Group.objects.filter(workspace=wf.workspace, pk=data.get("group") or 0).first()
    contact = Contact.objects.filter(workspace=wf.workspace, pk=data.get("contact") or 0).first()
    step = Step.objects.create(workflow=wf, key=key, kind=Step.RECIPIENT, order=after + 5, settings=settings,
                               recipient_group=group, recipient_contact=contact, **st)
    for b in data.get("bots") or []:
        b = _rename(b, names)
        cond = json.loads(json.dumps(b.get("condition") or {}).replace(f'"{old_key}"', f'"{key}"')) if old_key else b.get("condition") or {}
        Bot.objects.create(workflow=wf, key=new_key(), bot_type=b["bot_type"], name=b["name"], trigger=b["trigger"],
                           step_key=key, config=b.get("config") or {}, condition=cond, enabled=b.get("enabled", True),
                           run_once=b.get("run_once", False), success_tags=b.get("success_tags") or [],
                           delay_minutes=b.get("delay_minutes") or 0, order=wf.bots.count())
    StepTemplate.objects.filter(pk=tpl.pk).update(uses=tpl.uses + 1)
    return step


def summary(tpl):
    d = tpl.data or {}
    parts = [f"{len(d.get('fields') or [])} field{'s' if len(d.get('fields') or []) != 1 else ''}"]
    if d.get("bots"):
        parts.append(f"{len(d['bots'])} bot{'s' if len(d['bots']) != 1 else ''}")
    who = (d.get("step") or {}).get("recipient_email") or ""
    if who and not re.search(r"\{", who):
        parts.append(who)
    return " · ".join(parts)
