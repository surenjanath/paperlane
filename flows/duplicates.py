"""Duplicate check: flag a document when another live document of the same workflow has the same value in a
chosen field (e.g. two withdrawal requests for one policy). Workflow.options["duplicates"] =
{"fields": ["Policy Number"], "days": 0 (0 = only documents still in progress, N = also finished in the last N days),
 "notify": true}"""
import datetime as dt

from django.utils import timezone

TAG = "#Possible duplicate"


def config(workflow):
    cfg = (workflow.options or {}).get("duplicates") or {}
    fields = [f.strip() for f in cfg.get("fields") or [] if str(f).strip()]
    return {**cfg, "fields": fields} if fields else None


def _norm(v):
    return " ".join(str(v).split()).upper() if v not in (None, "", [], {}) else ""


def _lookup(values, name):
    if name in values:
        return values[name]
    low = name.lower()
    return next((v for k, v in values.items() if str(k).lower() == low), None)


def find(packet, cfg=None):
    """[{id, name, field, value, status}] — other documents that share a checked field value."""
    from .models import Packet

    cfg = cfg or config(packet.workflow)
    if not cfg or packet.is_test:
        return []
    mine = packet.flat_values()
    wanted = {f: _norm(_lookup(mine, f)) for f in cfg["fields"]}
    wanted = {f: v for f, v in wanted.items() if v}
    if not wanted:
        return []
    qs = Packet.objects.filter(workflow=packet.workflow, is_test=False, trashed_at__isnull=True).exclude(pk=packet.pk)
    days = int(cfg.get("days") or 0)
    if days:
        qs = qs.filter(created_at__gte=timezone.now() - dt.timedelta(days=days))
    else:
        qs = qs.filter(status=Packet.IN_PROGRESS)
    hits = []
    for other in qs.order_by("-created_at")[:1000]:
        vals = other.flat_values()
        for f, v in wanted.items():
            if _norm(_lookup(vals, f)) == v:
                hits.append({"id": other.pk, "name": other.name, "field": f, "value": str(_lookup(mine, f)),
                             "status": other.get_status_display()})
                break
    return hits


def check(packet, *, actor=None):
    """Re-check and flag (tag + comment + notification) when new duplicates appear. Returns the list."""
    from . import engine
    from .models import Comment, Tag

    cfg = config(packet.workflow)
    if not cfg:
        return []
    hits = find(packet, cfg)
    known = {d["id"] for d in (packet.flags or {}).get("duplicates", [])}
    new = [h for h in hits if h["id"] not in known]
    flags = dict(packet.flags or {})
    if hits:
        flags["duplicates"] = hits
    else:
        flags.pop("duplicates", None)
    if flags != (packet.flags or {}):
        packet.flags = flags
        packet.save(update_fields=["flags"])
    if not new:
        return hits
    tag, _ = Tag.objects.get_or_create(workspace=packet.workspace, name=TAG)
    packet.tags.add(tag)
    lines = "\n".join(f"• #{h['id']} {h['name']} ({h['status']}) — same {h['field']}: {h['value']}" for h in new)
    Comment.objects.create(packet=packet, author=None, author_label="Duplicate check",
                           text=f"This may duplicate another request:\n{lines}")
    engine.audit(packet.workspace, "packet.possible_duplicate", lines[:300], packet=packet)
    if cfg.get("notify", True) and packet.workflow.created_by:
        engine.notify(packet.workflow.created_by, packet.workspace, f"Possible duplicate: {packet.name}",
                      f"Same {new[0]['field']} as #{new[0]['id']} {new[0]['name']}", f"/documents/{packet.pk}/", kind="assigned")
    return hits
