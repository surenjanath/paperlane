"""Data retention: delete finished documents, emptied trash and old audit events after the periods a workspace sets.

Off by default (0 = keep forever). Documents on legal hold (packet.flags["legal_hold"]) are never deleted. Attached
files and sealed PDFs are removed from storage too, and every purge is recorded in the workspace audit trail."""
import datetime as dt

from django.db.models import Q
from django.utils import timezone

DEFAULTS = {"documents_days": 0, "statuses": ["completed", "declined", "cancelled"], "trash_days": 0, "audit_days": 0}


def policy(ws):
    return {**DEFAULTS, **((ws.settings or {}).get("retention") or {})}


def _held_ids(ws):
    """Documents on legal hold. (A plain ~Q(flags__legal_hold=True) would also drop documents with no flag at all,
    because the JSON lookup is NULL for them.)"""
    from .models import Packet

    return Packet.objects.filter(workspace=ws, flags__legal_hold=True).values("pk")


def due(ws, now=None):
    """What the policy would delete now: {"documents": qs, "trash": qs, "audit": qs} (only the enabled rules)."""
    from .models import AuditEvent, Packet

    now = now or timezone.now()
    p = policy(ws)
    out = {}
    if p["documents_days"]:
        cutoff = now - dt.timedelta(days=int(p["documents_days"]))
        out["documents"] = (Packet.objects.filter(workspace=ws, status__in=p["statuses"], trashed_at__isnull=True)
                            .filter(Q(completed_at__lt=cutoff) | Q(completed_at__isnull=True, last_activity_at__lt=cutoff))
                            .exclude(pk__in=_held_ids(ws)))
    if p["trash_days"]:
        out["trash"] = (Packet.objects.filter(workspace=ws, trashed_at__lt=now - dt.timedelta(days=int(p["trash_days"])))
                        .exclude(pk__in=_held_ids(ws)))
    if p["audit_days"]:
        out["audit"] = AuditEvent.objects.filter(workspace=ws, created_at__lt=now - dt.timedelta(days=int(p["audit_days"])))
    return out


def preview(ws, now=None):
    return {k: qs.count() for k, qs in due(ws, now).items()}


def _delete_packets(qs):
    from .models import DocumentSeal, PacketFile

    ids = list(qs.values_list("pk", flat=True))
    for f in PacketFile.objects.filter(packet_id__in=ids):
        f.file.delete(save=False)
    for s in DocumentSeal.objects.filter(packet_id__in=ids).exclude(pdf=""):
        s.pdf.delete(save=False)
    qs.model.objects.filter(pk__in=ids).delete()
    return ids


def apply(ws, now=None, dry_run=False):
    """Apply the workspace's policy. Returns {"documents": n, "trash": n, "audit": n}."""
    from .engine import audit

    result = {}
    for key, qs in due(ws, now).items():
        if dry_run:
            result[key] = qs.count()
            continue
        if key == "audit":
            n, _ = qs.delete()
            result[key] = n
        else:
            ids = _delete_packets(qs)
            result[key] = len(ids)
            if ids:
                audit(ws, "retention.purged", f"Deleted {len(ids)} document(s) by the {'trash' if key == 'trash' else 'retention'} rule: "
                      + ", ".join(f"#{i}" for i in ids[:50]) + ("…" if len(ids) > 50 else ""))
    if not dry_run and result.get("audit"):
        audit(ws, "retention.purged", f"Pruned {result['audit']} audit event(s) older than {policy(ws)['audit_days']} days")
    return result


def run_daily(now=None):
    """Called by the scheduler; runs at most once per day across all workspaces."""
    from django.core.cache import cache

    from accounts.models import Workspace

    today = timezone.localdate().isoformat()
    if cache.get("retention-day") == today:
        return 0
    cache.set("retention-day", today, 60 * 60 * 26)
    total = 0
    for ws in Workspace.objects.all():
        if any(policy(ws)[k] for k in ("documents_days", "trash_days", "audit_days")):
            total += sum(apply(ws, now).values())
    return total
