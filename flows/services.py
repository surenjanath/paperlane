"""Workflow definition services: publish, duplicate, restore versions, defaults."""
import copy
import json

from django.db import transaction
from django.utils import timezone

from .models import Bot, Step, Workflow, WorkflowDocument, WorkflowVersion, new_key


def blank_form_schema():
    return {"fields": [
        {"id": new_key(), "type": "heading", "label": "Heading", "text": "Description"},
        {"id": new_key(), "type": "text", "label": "Type a question", "name": "singlelinetext1",
         "placeholder": "Enter your answer here", "required": False, "role": "anyone"},
    ]}


@transaction.atomic
def create_workflow(workspace, user, *, name, kind=Workflow.WORKFLOW, with_document=True):
    wf = Workflow.objects.create(workspace=workspace, name=name, kind=kind, created_by=user,
                                 access=Workflow.PRIVATE)
    Step.objects.create(workflow=wf, order=10, name="Recipient 1",
                        recipient_type=Step.R_CURRENT if kind == Workflow.TEMPLATE else Step.R_EMAIL)
    if with_document:
        WorkflowDocument.objects.create(workflow=wf, name="Untitled form", schema=blank_form_schema())
    return wf


@transaction.atomic
def publish(workflow, user):
    snapshot = workflow.build_snapshot()
    if workflow.status == Workflow.PUBLISHED:
        last = workflow.published_snapshot or {}
        if json.dumps(_strip(last), sort_keys=True) == json.dumps(_strip(snapshot), sort_keys=True):
            workflow.has_unpublished_changes = False
            workflow.save(update_fields=["has_unpublished_changes"])
            return workflow
        workflow.version += 1
        snapshot["version"] = workflow.version
    workflow.status = Workflow.PUBLISHED
    workflow.published_snapshot = snapshot
    workflow.published_at = timezone.now()
    workflow.has_unpublished_changes = False
    workflow.save()
    WorkflowVersion.objects.create(workflow=workflow, number=workflow.version, snapshot=snapshot, published_by=user)
    return workflow


def _strip(snap):
    snap = dict(snap or {})
    snap.pop("version", None)
    # file_name mirrors file_url; ignore it so snapshots published before it existed don't look changed
    snap["documents"] = [{k: v for k, v in d.items() if k != "file_name"} for d in snap.get("documents", [])]
    return snap


def _remap(text_or_obj, mapping):
    raw = json.dumps(text_or_obj)
    for old, new in mapping.items():
        raw = raw.replace(f'"{old}"', f'"{new}"')
    return json.loads(raw)


@transaction.atomic
def load_snapshot(workflow, snapshot, *, fresh_keys=False):
    """Replace the editable definition of ``workflow`` with ``snapshot``.

    Uploaded reference files stay with the document key that held them, and contact recipients are matched back
    by id (or email) inside the same workspace."""
    from accounts.models import Contact, Group

    files = {d.key: d.file for d in workflow.documents.all() if d.file}
    contacts = Contact.objects.filter(workspace=workflow.workspace)
    groups = set(Group.objects.filter(workspace=workflow.workspace).values_list("pk", flat=True))
    workflow.steps.all().delete()
    workflow.documents.all().delete()
    workflow.bots.all().delete()
    mapping = {}
    if fresh_keys:
        for item in snapshot.get("steps", []) + snapshot.get("documents", []) + snapshot.get("bots", []):
            mapping[item["key"]] = new_key()
        snapshot = _remap(snapshot, mapping)
    for i, d in enumerate(snapshot.get("documents", [])):
        WorkflowDocument.objects.create(
            workflow=workflow, key=d["key"], name=d["name"], kind=d.get("kind", "form"), order=i,
            schema={"fields": d.get("fields", []), "roles": d.get("roles", [])}, design=d.get("design", {}),
            condition=d.get("condition", {}), file=files.get(d["key"]) or "")
    for s in snapshot.get("steps", []):
        rc = s.get("recipient_contact") or {}
        contact = None
        if rc.get("id"):  # the id must agree with the email, so an id from another workspace's file can't misfire
            contact = contacts.filter(pk=rc["id"], **({"email__iexact": rc["email"]} if rc.get("email") else {})).first()
        if contact is None and rc.get("email"):
            contact = contacts.filter(email__iexact=rc["email"]).first()
        Step.objects.create(
            workflow=workflow, key=s["key"], order=s.get("order", 0), kind=s["kind"], name=s["name"],
            recipient_type=s.get("recipient_type", Step.R_EMAIL), recipient_email=s.get("recipient_email", ""),
            recipient_name=s.get("recipient_name", ""), recipient_contact=contact,
            # a group from another workspace (an imported file) is dropped, never linked
            recipient_group_id=(s.get("recipient_group") or {}).get("id") if (s.get("recipient_group") or {}).get("id") in groups else None,
            message_subject=s.get("message_subject", ""), message_body=s.get("message_body", ""),
            attach_documents=s.get("attach_documents", False), document_access=s.get("document_access", {}),
            revoke_on_complete=s.get("revoke_on_complete", False), thank_you=s.get("thank_you", {}),
            condition=s.get("condition", {}), branches=s.get("branches", []), settings=s.get("settings", {}))
    for i, b in enumerate(snapshot.get("bots", [])):
        Bot.objects.create(workflow=workflow, key=b["key"], bot_type=b["bot_type"], name=b["name"],
                           trigger=b["trigger"], step_key=b.get("step_key", ""), config=b.get("config", {}),
                           condition=b.get("condition", {}), order=i, enabled=b.get("enabled", True),
                           run_once=b.get("run_once", False), success_tags=b.get("success_tags", []),
                           delay_minutes=b.get("delay_minutes", 0))
    workflow.has_unpublished_changes = True
    workflow.save(update_fields=["has_unpublished_changes", "updated_at"])
    return workflow


@transaction.atomic
def duplicate(workflow, user, *, name=None):
    clone = Workflow.objects.create(
        workspace=workflow.workspace, kind=workflow.kind, name=name or f"{workflow.name} (copy)",
        description=workflow.description, created_by=user, access=workflow.access,
        welcome_enabled=workflow.welcome_enabled, welcome_message=workflow.welcome_message,
        esign_consent=workflow.esign_consent, autostart=copy.deepcopy(workflow.autostart or {}))
    clone.tags.set(workflow.tags.all())
    load_snapshot(clone, workflow.export_snapshot(), fresh_keys=True)
    # copy uploaded reference files
    for src, dst in zip(workflow.documents.order_by("order", "id"), clone.documents.order_by("order", "id")):
        if src.file:
            dst.file = src.file
            dst.save(update_fields=["file"])
    return clone


def renumber_steps(workflow):
    for i, st in enumerate(workflow.steps.order_by("order", "id")):
        new = (i + 1) * 10
        if st.order != new:
            st.order = new
            st.save(update_fields=["order"])


def field_choices(workflow):
    """[(field_id, "Document · Label")] across documents, for condition/mapping pickers."""
    out = []
    for doc in workflow.documents.all():
        for f in doc.input_fields():
            out.append((f["id"], f"{doc.name} · {f.get('label') or f['id']}", f.get("name") or f.get("label") or f["id"],
                        f.get("options", [])))
    return out
