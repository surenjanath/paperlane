"""Watchdog: flags documents that look unusual compared with the rest of their workflow, after each step.

Findings come from statistics, not from a model, so they can't be invented:
- an amount far outside what this workflow usually sees (robust z-score on median and MAD, needs 8+ earlier documents);
- the same person, email or phone appearing in several documents of this workflow in the last 90 days.
New findings are kept in ``packet.flags["watchdog"]``, shown on the document, and sent once to its owner.
Turn it on in AI & models (Watchdog). Sensitive fields are never examined.
"""
import datetime as dt
import hashlib
import statistics

from django.urls import reverse
from django.utils import timezone

MIN_HISTORY = 8
Z_LIMIT = 3.5
REPEAT_DAYS = 90
REPEAT_COUNT = 3
IDENTITY_TYPES = ("email", "phone")
IDENTITY_WORDS = ("name", "email", "phone", "policy", "account", "id number", "national id", "claimant", "customer")


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _fields(packet):
    for doc in packet.documents:
        vals = (packet.data or {}).get(doc["key"], {})
        for f in doc.get("fields", []):
            if f.get("sensitive") or f["id"] not in vals or vals[f["id"]] in (None, "", [], {}):
                continue
            yield doc["key"], f, vals[f["id"]]


def _others(packet, days=None):
    from .models import Packet

    qs = Packet.objects.filter(workflow=packet.workflow, trashed_at__isnull=True, is_test=False).exclude(pk=packet.pk)
    if days:
        qs = qs.filter(created_at__gte=timezone.now() - dt.timedelta(days=days))
    return qs.order_by("-created_at")


def inspect(packet):
    """New findings for ``packet`` (list of {"kind", "field", "message"}); stores and announces them."""
    from . import ai_actions

    if not ai_actions.settings_for(packet.workspace).get("watchdog"):
        return []
    found = []
    history = list(_others(packet).values_list("data", flat=True)[:200])
    recent = list(_others(packet, REPEAT_DAYS).values_list("data", flat=True)[:500])
    for doc_key, f, value in _fields(packet):
        label = f.get("label") or f.get("name") or f["id"]
        if f.get("type") in ("number", "currency"):
            x = _num(value)
            past = [n for n in (_num((d or {}).get(doc_key, {}).get(f["id"])) for d in history) if n is not None]
            if x is not None and len(past) >= MIN_HISTORY:
                med = statistics.median(past)
                mad = statistics.median([abs(p - med) for p in past]) or (statistics.pstdev(past) or 0)
                if mad:
                    z = 0.6745 * (x - med) / mad
                    if abs(z) > Z_LIMIT:
                        found.append({"kind": "amount", "field": label, "message":
                                      f"{label} is {x:,.2f}, far {'above' if z > 0 else 'below'} the usual "
                                      f"{med:,.2f} for this workflow ({len(past)} earlier documents)."})
        elif f.get("type") in IDENTITY_TYPES or any(w in label.lower() for w in IDENTITY_WORDS):
            key = str(value).strip().lower()
            if len(key) < 4:
                continue
            same = sum(1 for d in recent if str((d or {}).get(doc_key, {}).get(f["id"]) or "").strip().lower() == key)
            if same + 1 >= REPEAT_COUNT:
                found.append({"kind": "repeat", "field": label, "message":
                              f"The same {label.lower()} appears in {same + 1} documents of this workflow in the last {REPEAT_DAYS} days."})
    return _store(packet, found)


def _store(packet, found):
    from . import engine

    flags = dict(packet.flags or {})
    known = {w.get("id") for w in flags.get("watchdog") or []}
    new = []
    for f in found:
        f["id"] = hashlib.sha1(f"{f['kind']}|{f['field']}|{f['message']}".encode()).hexdigest()[:12]
        if f["id"] not in known:
            f["at"] = timezone.now().isoformat()
            new.append(f)
    if not new:
        return []
    flags["watchdog"] = (flags.get("watchdog") or []) + new
    packet.flags = flags
    packet.save(update_fields=["flags"])
    for f in new:
        engine.audit(packet.workspace, "watchdog.flag", f["message"], packet=packet)
    owner = packet.created_by or packet.workflow.created_by
    if owner:
        engine.notify(owner, packet.workspace, f"Watchdog: {packet.name} looks unusual", new[0]["message"],
                      reverse("flows:packet_detail", args=[packet.pk]), kind="attention")
    return new


def dismiss(packet, finding_id):
    flags = dict(packet.flags or {})
    for w in flags.get("watchdog") or []:
        if w.get("id") == finding_id:
            w["dismissed"] = True
    packet.flags = flags
    packet.save(update_fields=["flags"])
