"""Field-level history: every change to a document's values is recorded with who/what made it.

A pre_save hook on Packet diffs the stored data against the new data, so every write path (steps, bots, AI, revisions,
API) is covered. Callers describe the change with ``changed_by(actor, source)``; without it the change is "System"."""
import contextlib
import contextvars
import json

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

_ACTOR = contextvars.ContextVar("paperlane_change_actor", default=(None, "", ""))  # (user, label, source)
SKIP = {"signature", "initials"}


@contextlib.contextmanager
def changed_by(user=None, label="", source=""):
    token = _ACTOR.set((user if getattr(user, "pk", None) else None, label, source))
    try:
        yield
    finally:
        _ACTOR.reset(token)


def _text(v):
    if v in (None, "", [], {}):
        return ""
    if isinstance(v, (list, dict)):
        if isinstance(v, list) and v and isinstance(v[0], dict) and ("file_id" in v[0] or "name" in v[0]):
            return ", ".join(str(x.get("name") or "file") for x in v)
        return json.dumps(v, ensure_ascii=False)[:500]
    return str(v)[:1000]


def diff(documents, old, new):
    from .privacy import mask

    fields = {(d["key"], f["id"]): f for d in documents for f in d.get("fields", [])}
    out = []
    for dk in set(old) | set(new):
        a, b = old.get(dk) or {}, new.get(dk) or {}
        for fid in set(a) | set(b):
            if a.get(fid) == b.get(fid):
                continue
            f = fields.get((dk, fid), {})
            if f.get("type") in SKIP:
                ov, nv = ("signed" if a.get(fid) else ""), ("signed" if b.get(fid) else "")
            else:
                ov, nv = _text(a.get(fid)), _text(b.get(fid))
                if f.get("sensitive"):
                    ov, nv = (mask(ov) if ov else ""), (mask(nv) if nv else "")
            if ov == nv:
                continue
            out.append((dk, fid, (f.get("label") or f.get("name") or fid)[:200], ov, nv))
    return out


@receiver(pre_save, sender="flows.Packet")
def _remember(sender, instance, **kwargs):
    instance._history_old = None
    if instance.pk:
        instance._history_old = sender.objects.filter(pk=instance.pk).values_list("data", flat=True).first()


@receiver(post_save, sender="flows.Packet")
def _record(sender, instance, created, **kwargs):
    from .models import FieldChange

    old = {} if created else getattr(instance, "_history_old", None)
    if old is None:
        return
    changes = diff(instance.documents, old or {}, instance.data or {})
    if not changes:
        return
    user, label, source = _ACTOR.get()
    if created:
        source = source or "Started"
    FieldChange.objects.bulk_create([FieldChange(packet=instance, doc_key=str(dk)[:20], field_id=str(fid)[:128], label=str(lb)[:200],
                                                 old=ov, new=nv, actor=user, source=(source or "")[:120],
                                                 actor_label=(label or (user.get_full_name() or user.email if user else "System"))[:200])
                                     for dk, fid, lb, ov, nv in changes])
