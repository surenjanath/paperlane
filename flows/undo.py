"""Undo and redo for every change made in the workflow builder.

Before a builder POST changes anything, ``record`` saves the workflow's current definition (documents, steps, bots,
name, description, autostart) on the undo stack. ``undo`` puts the current definition on the redo stack and restores
the newest undo entry; ``redo`` does the reverse. A request that changed nothing leaves an entry equal to the current
state; those are skipped, so one click always undoes one visible change. Uploaded files and contact recipients
survive because restoring goes through ``services.load_snapshot``.
"""
import json

from django.db import transaction

from .models import BuilderSnapshot

LIMIT = 30

LABELS = {
    "step_add": "Add a step",
    "step_settings": "Change step settings",
    "autostart": "Change autostart",
    "doc_add": "Add a document",
    "doc_condition": "Change when a document shows",
    "bot_new": "Add a bot",
    "bot_edit": "Change a bot",
    "ai_workflow": "AI change",
    "replace_recipient": "Replace a recipient",
    "discard_draft": "Discard changes",
}
ACTION_LABELS = {
    "step_action": {"delete": "Delete a step", "up": "Move a step", "down": "Move a step", "duplicate": "Duplicate a step",
                    "rename": "Rename a step", "save_template": ""},
    "doc_action": {"rename": "Rename a document", "copy": "Copy a document", "up": "Move a document",
                   "down": "Move a document", "remove": "Remove a document", "condition": "Change when a document shows"},
    "bot_action": {"delete": "Delete a bot", "toggle": "Turn a bot on or off"},
}


def label_for(request):
    """The undo label for this builder request, or "" when the request never changes the definition."""
    match = getattr(request, "resolver_match", None)
    if match is None or request.method != "POST":
        return ""
    name = match.url_name
    if name == "ai_workflow":
        return "AI change" if request.POST.get("do") == "apply" else ""
    if name in ACTION_LABELS:
        return ACTION_LABELS[name].get(match.kwargs.get("action"), "Change")
    return LABELS.get(name, "")


def capture(wf):
    snap = wf.export_snapshot()
    for d in snap.get("documents", []):
        d.pop("file_url", None)
        d.pop("file_name", None)
    snap.pop("version", None)
    return {"snapshot": snap, "name": wf.name, "description": wf.description, "autostart": wf.autostart or {}}


def _same(a, b):
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def _top(wf, stack):
    return wf.builder_snapshots.filter(stack=stack).order_by("-id").first()


def record(wf, user, label):
    """Save the definition before a change. Clears redo: a new change starts a new branch of history."""
    state = capture(wf)
    top = _top(wf, BuilderSnapshot.UNDO)
    if top is not None and _same(top.data, state):
        top.label = label[:120]  # the same state: keep one entry, named after the latest attempt
        top.save(update_fields=["label"])
        return
    BuilderSnapshot.objects.create(workflow=wf, stack=BuilderSnapshot.UNDO, label=label[:120], data=state,
                                   created_by=user if getattr(user, "pk", None) else None)
    wf.builder_snapshots.filter(stack=BuilderSnapshot.REDO).delete()
    stale = list(wf.builder_snapshots.filter(stack=BuilderSnapshot.UNDO).order_by("-id").values_list("id", flat=True)[LIMIT:])
    if stale:
        BuilderSnapshot.objects.filter(id__in=stale).delete()


def restore(wf, data):
    from . import services

    services.load_snapshot(wf, data["snapshot"])
    wf.name = data.get("name") or wf.name
    wf.description = data.get("description", wf.description)
    wf.autostart = data.get("autostart") or {}
    wf.has_unpublished_changes = True
    if (wf.options or {}).get("ai_undo"):  # the AI panel's own undo would now restore a stale state
        wf.options = {k: v for k, v in wf.options.items() if k != "ai_undo"}
    wf.save(update_fields=["name", "description", "autostart", "options", "has_unpublished_changes", "updated_at"])


@transaction.atomic
def _move(wf, user, source, target):
    current = capture(wf)
    entries = wf.builder_snapshots.filter(stack=source).order_by("-id")
    for entry in entries:
        if _same(entry.data, current):  # a request that changed nothing
            entry.delete()
            continue
        BuilderSnapshot.objects.create(workflow=wf, stack=target, label=entry.label, data=current,
                                       created_by=user if getattr(user, "pk", None) else None)
        restore(wf, entry.data)
        label = entry.label
        entry.delete()
        return label or "Change"
    return ""


def undo(wf, user=None):
    """Restore the state before the latest change. Returns its label, or "" when there is nothing to undo."""
    return _move(wf, user, BuilderSnapshot.UNDO, BuilderSnapshot.REDO)


def redo(wf, user=None):
    return _move(wf, user, BuilderSnapshot.REDO, BuilderSnapshot.UNDO)


def state(wf):
    """Labels for the builder's Undo / Redo buttons ("" when the stack is empty)."""
    u, r = _top(wf, BuilderSnapshot.UNDO), _top(wf, BuilderSnapshot.REDO)
    return {"undo": u.label if u else "", "redo": r.label if r else ""}
