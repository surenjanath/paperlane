"""Workflow autostarts: several per workflow, each scheduled or event driven.

``Workflow.autostart`` = {"items": [item, ...]}. Item types:

* ``schedule`` – Scheduled start: ``start`` (local "YYYY-MM-DDTHH:MM"), ``tz``, ``repeat`` = once | multiple | schedule.
  multiple: ``every`` + ``unit`` (minutes/hours/days/weeks). schedule: ``schedule_type`` = week | month | daily | custom,
  ``months`` [1-12], ``weekdays`` [0=Mon…6], ``monthdays`` [1-31], ``time`` "HH:MM".
  ``stop`` = never | date | count (+ ``stop_date`` / ``stop_count``), ``notify_admin``.
* ``sheet`` – Scheduled start from an Excel/CSV file: the schedule fields plus ``file`` (storage path) and
  ``row_mode`` = all (one document per row every run) | next (the next unused row per run, ``next_row``).
* ``workflow`` – From another workflow: ``source_workflow`` (pk); fields with matching names are pre-filled.
* ``email`` – From an email: ``token`` (the address), ``senders`` = members | domains (+ ``domains``), ``ai_fill``.
  See flows/inbound.py.
* ``webhook`` – From external event (also used for integration sources such as Google Sheets or Salesforce,
  which POST to the workflow's webhook URL through a Connected account / automation tool): ``source`` label.

Runtime state per item: ``count`` (runs so far) and ``last_run`` (ISO, UTC).
"""
import csv
import datetime as dt
import io
import secrets
import zoneinfo

from django.conf import settings
from django.core.files.storage import default_storage
from django.utils import timezone

TYPES = {
    "schedule": "Scheduled start",
    "sheet": "Scheduled start from Excel file",
    "workflow": "From another workflow",
    "webhook": "From external event",
    "email": "From an email",
}
# Integration sources from the live catalog; they start through the webhook (``webhook`` items with ``source``).
INTEGRATION_SOURCES = ["Google Sheets update", "Salesforce", "MS Dynamics 365", "NetSuite", "SharePoint",
                       "Microsoft 365 Excel update", "Scheduled start from Google Sheets", "Airtable update"]
UNITS = {"minutes": 60, "hours": 3600, "days": 86400, "weeks": 604800}
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


def new_id():
    return secrets.token_hex(4)


def _legacy(auto):
    """Convert the single-trigger format ({"type": ..., "schedule": {...}}) to an item."""
    kind = auto.get("type")
    if not kind:
        return None
    item = {"id": "legacy", "type": kind, "enabled": True}
    if kind == "schedule":
        s = auto.get("schedule") or {}
        freq = s.get("frequency", "daily")
        item.update(start="", tz=settings.TIME_ZONE, time=s.get("time", "09:00"), stop="never")
        if freq == "hourly":
            item.update(repeat="multiple", every=1, unit="hours")
        else:
            item.update(repeat="schedule", months=list(range(1, 13)),
                        schedule_type={"weekly": "week", "monthly": "month"}.get(freq, "daily"),
                        weekdays=[int(s.get("weekday", 0))], monthdays=[int(s.get("day", 1))])
    elif kind == "workflow":
        item["source_workflow"] = auto.get("source_workflow")
    return item


def items(wf):
    auto = wf.autostart or {}
    if "items" in auto:
        return [dict(i) for i in auto["items"]]
    legacy = _legacy(auto)
    return [legacy] if legacy else []


def save_items(wf, new_items):
    wf.autostart = {"items": new_items} if new_items else {}
    wf.save(update_fields=["autostart"])


def trigger_label(item, source_names=None):
    """Short name of what starts the workflow: a form is handled separately; this covers autostart items."""
    kind = item.get("type")
    if kind == "webhook":
        return item.get("source") or "External event"
    if kind == "schedule":
        return "Scheduled start"
    if kind == "sheet":
        return "Excel file"
    if kind == "email":
        return "Email"
    if kind == "workflow":
        try:
            pk = int(item.get("source_workflow") or 0)
        except (TypeError, ValueError):
            pk = 0
        name = (source_names or {}).get(pk)
        return f"From {name}" if name else "From another workflow"
    return TYPES.get(kind, kind or "Autostart")


def describe(item, source_names=None):
    """One-line summary for the Start node."""
    t = item.get("type")
    if t in ("schedule", "sheet"):
        prefix = "📄 Excel rows · " if t == "sheet" else "⏱ "
        rep = item.get("repeat", "once")
        if rep == "once":
            return f"{prefix}Once on {(item.get('start') or 'start date not set').replace('T', ' ')}"
        if rep == "multiple":
            return f"{prefix}Every {item.get('every', 1)} {item.get('unit', 'hours')}"
        st = item.get("schedule_type", "daily")
        when = {"daily": "Every day", "week": ", ".join(WEEKDAYS[d][:3] for d in item.get("weekdays") or []) or "Weekly",
                "month": "Days " + ", ".join(str(d) for d in item.get("monthdays") or []) if item.get("monthdays") else "Monthly",
                "custom": "Custom schedule"}.get(st, st)
        return f"{prefix}{when} at {item.get('time', '09:00')}"
    if t == "workflow":
        return f"↪ When “{(source_names or {}).get(item.get('source_workflow'), '(deleted workflow)')}” completes"
    if t == "webhook":
        return f"⚡ {item.get('source') or 'From external event'}"
    if t == "email":
        from .inbound import address

        return f"✉ {address(item) or 'From an email (set PAPERLANE_INBOUND_DOMAIN)'}"
    return t or ""


# ---- scheduling ----------------------------------------------------------------------------
def _tz(item):
    try:
        return zoneinfo.ZoneInfo(item.get("tz") or settings.TIME_ZONE)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return zoneinfo.ZoneInfo(settings.TIME_ZONE)


def _parse_local(value, tz):
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(value, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    return None


def _hm(value, default=(9, 0)):
    try:
        h, m = [int(x) for x in str(value or "").split(":")[:2]]
        return h, m
    except ValueError:
        return default


def _day_matches(item, d):
    st = item.get("schedule_type", "daily")
    months = item.get("months") or list(range(1, 13))
    if d.month not in [int(m) for m in months]:
        return False
    wds = [int(x) for x in item.get("weekdays") or []]
    mds = [int(x) for x in item.get("monthdays") or []]
    if st == "daily":
        return True
    if st == "week":
        return d.weekday() in wds
    if st == "month":
        return d.day in mds
    # custom: every selected constraint must hold (unset ones allow all)
    return (not wds or d.weekday() in wds) and (not mds or d.day in mds)


def stopped(item, now):
    stop = item.get("stop", "never")
    if stop == "count" and int(item.get("count", 0)) >= int(item.get("stop_count") or 0) > 0:
        return True
    if stop == "date":
        end = _parse_local(item.get("stop_date"), _tz(item))
        if end is not None and len(str(item.get("stop_date"))) <= 10:
            end = end + dt.timedelta(days=1)  # a date stops at the end of that day
        if end is not None and now >= end:
            return True
    return False


def is_due(item, now=None):
    """Should this scheduled item start now? ``now`` is an aware datetime (defaults to timezone.now())."""
    if item.get("type") not in ("schedule", "sheet") or not item.get("enabled", True):
        return False
    now = now or timezone.now()
    tz = _tz(item)
    local = now.astimezone(tz)
    start = _parse_local(item.get("start"), tz)
    if start is not None and local < start:
        return False
    if stopped(item, now):
        return False
    last = dt.datetime.fromisoformat(item["last_run"]) if item.get("last_run") else None
    rep = item.get("repeat", "once")
    if rep == "once":
        return int(item.get("count", 0)) == 0 and start is not None
    if rep == "multiple":
        step = dt.timedelta(seconds=max(1, int(item.get("every") or 1)) * UNITS.get(item.get("unit", "hours"), 3600))
        return last is None or now - last >= step
    # on schedule
    if not _day_matches(item, local.date()):
        return False
    h, m = _hm(item.get("time"))
    due = local.replace(hour=h, minute=m, second=0, microsecond=0)
    return local >= due and (last is None or last.astimezone(tz) < due)


# ---- spreadsheet source ----------------------------------------------------------------------
def store_sheet(wf, upload):
    name = f"autostart/{wf.pk}/{secrets.token_hex(4)}-{upload.name}"
    return default_storage.save(name, upload)


def read_sheet(path_or_file, filename=None):
    """Rows of an uploaded CSV / XLSX as dicts keyed by the header row (empty rows skipped)."""
    f = default_storage.open(path_or_file, "rb") if isinstance(path_or_file, str) else path_or_file
    name = (filename or getattr(f, "name", "") or str(path_or_file)).lower()
    try:
        data = f.read()
    finally:
        if isinstance(path_or_file, str):
            f.close()
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook

        ws = load_workbook(io.BytesIO(data), read_only=True, data_only=True).active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        header = [str(h).strip() if h is not None else "" for h in rows[0]]
        out = []
        for r in rows[1:]:
            row = {h: ("" if v is None else (v.isoformat()[:10] if isinstance(v, (dt.date, dt.datetime)) else str(v)))
                   for h, v in zip(header, r) if h}
            if any(row.values()):
                out.append(row)
        return out
    text = data.decode("utf-8-sig", errors="replace")
    return [r for r in csv.DictReader(io.StringIO(text)) if any((v or "").strip() for v in r.values())]


# ---- running -------------------------------------------------------------------------------
def _start_from_row(wf, row, user, source):
    from .engine import start_packet

    row = {k: v for k, v in row.items() if k and v not in (None, "")}
    recipient = row.pop("recipient", "") or row.pop("Recipient", "")
    assignee = None
    if recipient:
        from django.contrib.auth import get_user_model

        assignee = {"email": recipient, "name": row.pop("recipient_name", recipient),
                    "user": get_user_model().objects.filter(email__iexact=recipient).first()}
    packet = start_packet(wf, user=user, source=source, prefill=row, first_assignee=assignee,
                          name=row.pop("document_name", None))
    if assignee:
        from .views.library import _send_invites

        _send_invites(packet)
    return packet


def run_item(wf, item, now=None):
    """Start the documents for one due item. Returns the number of documents started."""
    from .engine import audit, notify, start_packet

    now = now or timezone.now()
    owner = wf.created_by
    started = 0
    if item["type"] == "sheet":
        try:
            rows = read_sheet(item["file"])
        except Exception as exc:  # unreadable file: record it and keep the schedule going
            audit(wf.workspace, "autostart.failed", f"{wf.name}: could not read the spreadsheet ({exc})")
            rows = []
        if item.get("row_mode") == "next":
            i = int(item.get("next_row", 0))
            if i < len(rows):
                _start_from_row(wf, rows[i], owner, "autostart")
                item["next_row"] = i + 1
                started = 1
        else:
            for row in rows:
                _start_from_row(wf, row, owner, "autostart")
                started += 1
    else:
        start_packet(wf, user=owner, source="autostart")
        started = 1
    item["count"] = int(item.get("count", 0)) + 1
    item["last_run"] = now.isoformat()
    if owner and (item.get("notify_admin") or item.get("id") == "legacy"):
        notify(owner, wf.workspace, f"Started automatically: {wf.name}",
               f"{TYPES.get(item['type'], 'Autostart')} sent {started} document{'s' if started != 1 else ''}.",
               "", kind="autostart")
    return started


def run_due(now=None):
    """Run every due scheduled autostart of published workflows. Returns documents started."""
    from .models import Workflow

    now = now or timezone.now()
    total = 0
    for wf in Workflow.objects.live().filter(status=Workflow.PUBLISHED).exclude(autostart={}):
        its = items(wf)
        changed = False
        for item in its:
            if is_due(item, now):
                total += run_item(wf, item, now)
                changed = True
        if changed:
            wf.last_autostart_at = now
            wf.autostart = {"items": its}
            wf.save(update_fields=["autostart", "last_autostart_at"])
    return total


def chained_targets(workspace, source_pk):
    """Published workflows with a "From another workflow" autostart on ``source_pk``."""
    from .models import Workflow

    out = []
    for wf in Workflow.objects.live().filter(workspace=workspace, status=Workflow.PUBLISHED).exclude(autostart={}):
        if any(i.get("type") == "workflow" and i.get("enabled", True) and str(i.get("source_workflow")) == str(source_pk)
               for i in items(wf)):
            out.append(wf)
    return out
