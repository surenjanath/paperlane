"""Key dates: dates that matter after a document is finished (contract end, renewal, policy expiry, next review).

A workflow lists its key-date fields in ``wf.options["key_dates"]``::

    [{"field": "<field id>", "label": "Contract end", "remind": [30, 7, 0], "emails": "legal@x.com"}]

When a document completes, each filled field becomes a ``KeyDate``. The scheduler (``bots.tick``) sends each reminder
once — N days before the date — to whoever started the document plus the listed addresses, and posts an in-app
notification. Turning the setting on also captures documents that already completed.
"""
import datetime as dt
import re

from django.urls import reverse
from django.utils import timezone

DEFAULT_REMIND = [30, 7, 0]
SUGGEST = re.compile(r"expir|renew|end\b|end date|until|valid|due|deadline|review|termin|anniversar|maturity|lapse", re.I)


def date_fields(wf):
    """[(field id, label, suggested?)] for every date field in the workflow's documents."""
    out = []
    for d in wf.documents.order_by("order", "id"):
        for f in d.input_fields():
            if f.get("type") == "date":
                label = f.get("label") or f.get("name") or f["id"]
                out.append((f["id"], label, bool(SUGGEST.search(f"{label} {f.get('name') or ''}"))))
    return out


def config(wf):
    return [c for c in (wf.options or {}).get("key_dates") or [] if c.get("field")]


def clean_days(raw):
    days = set()
    for part in re.split(r"[,\s]+", str(raw or "")):
        if part.strip().lstrip("-").isdigit():
            days.add(max(0, min(int(part), 3650)))
    return sorted(days, reverse=True) or list(DEFAULT_REMIND)


def parse(value):
    from .conditions import _when

    if isinstance(value, dt.date):
        return value
    w = _when(value)
    if not w:
        return None
    kind, v = w
    if kind == "date":
        return v
    if kind == "datetime":
        return v.date()
    return None


def capture(packet):
    """Create the key dates of a completed document (idempotent). Returns how many exist for it."""
    from .models import KeyDate

    cfg = config(packet.workflow)
    if not cfg:
        return 0
    flat = {}  # every saved value by field id (also fields added to the workflow after the document started)
    for values in (packet.data or {}).values():
        if isinstance(values, dict):
            flat.update({k: v for k, v in values.items() if v not in (None, "")})
    for c in cfg:
        when = parse(flat.get(c["field"]))
        if not when:
            continue
        KeyDate.objects.update_or_create(packet=packet, field_id=c["field"], defaults={
            "workspace": packet.workspace, "label": (c.get("label") or c["field"])[:160], "date": when,
            "remind_days": c.get("remind") or list(DEFAULT_REMIND), "notify_emails": (c.get("emails") or "")[:500]})
    return packet.key_dates.count()


def backfill(wf):
    from .models import Packet

    n = 0
    for p in wf.packets.filter(status=Packet.COMPLETED, trashed_at__isnull=True, is_test=False):
        n += 1 if capture(p) else 0
    return n


def _phrase(days):
    return "today" if days == 0 else ("tomorrow" if days == 1 else f"in {days} days")


def run_due(now=None):
    """Send the reminders that are due. Returns how many went out."""
    from . import engine, mailer
    from .models import KeyDate

    today = timezone.localdate(now or timezone.now())
    sent = 0
    horizon = today + dt.timedelta(days=3650)
    for k in KeyDate.objects.filter(done_at__isnull=True, date__gte=today, date__lte=horizon) \
            .select_related("packet", "packet__created_by", "workspace"):
        left = (k.date - today).days
        due = [d for d in (k.remind_days or []) if d >= left and d not in (k.sent or [])]
        if not due:
            continue
        p = k.packet
        link = engine.absolute(reverse("flows:packet_detail", args=[p.pk]))
        subject = f"{k.label} {_phrase(left)}: {p.name}"
        message = (f"{k.label} for “{p.name}” is {_phrase(left)} ({k.date:%d %b %Y}).\n\nOpen the document: {link}\n\n"
                   f"See every upcoming date: {engine.absolute(reverse('flows:key_dates'))}")
        to = [e.strip() for e in (k.notify_emails or "").split(",") if "@" in e]
        if p.created_by and p.created_by.email:
            to.append(p.created_by.email)
            engine.notify(p.created_by, k.workspace, subject, "", reverse("flows:packet_detail", args=[p.pk]), kind="reminder")
        if to:
            mailer.send(k.workspace, sorted(set(to)), "custom", {"subject": subject, "message": message},
                        packet=p, subject=subject, body=message)
        k.sent = sorted(set((k.sent or []) + due), reverse=True)  # one email even if several reminders came due at once
        k.save(update_fields=["sent"])
        engine.audit(k.workspace, "keydate.reminder", subject, packet=p)
        sent += 1
    return sent
