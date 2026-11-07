"""Email digests: a personal digest (what's waiting for me) and a team report for managers. Opt-in per person in
My account › Notifications ("off" / "daily" / "weekly"); sent by the scheduler after 07:00 local time, weekly ones on
Mondays, never twice the same day, and skipped when there's nothing to say."""
import datetime as dt

from django.db.models import Count, Min
from django.utils import timezone

from . import mailer
from .engine import absolute


def _fmt(d):
    return timezone.localtime(d).strftime("%b %d, %H:%M") if d else ""


def personal(user, ws, since):
    from core.queries import my_runs

    from .models import Packet

    now = timezone.now()
    runs = list(my_runs(user, ws).filter(status__in=["pending", "opened"], packet__status=Packet.IN_PROGRESS)
                .select_related("packet").order_by("due_at", "created_at")[:50])
    overdue = [r for r in runs if r.due_at and r.due_at < now]
    soon = [r for r in runs if r.due_at and now <= r.due_at < now + dt.timedelta(hours=24)]
    mine = Packet.objects.filter(workspace=ws, created_by=user, last_activity_at__gte=since)
    done = list(mine.filter(status=Packet.COMPLETED, completed_at__gte=since)[:20])
    declined = list(mine.filter(status=Packet.DECLINED)[:20])
    if not (runs or done or declined):
        return None
    lines = [f"Hi {user.first_name or user.email},", ""]
    if runs:
        lines.append(f"WAITING FOR YOU ({len(runs)})" + (f" — {len(overdue)} overdue" if overdue else ""))
        for r in runs[:15]:
            tag = " [OVERDUE]" if r in overdue else (" [due today]" if r in soon else "")
            lines.append(f"• {r.packet.name} — {r.step_name}{tag}\n  {absolute(r.packet.get_absolute_url())}")
        if len(runs) > 15:
            lines.append(f"…and {len(runs) - 15} more in your Inbox: {absolute('/inbox/')}")
        lines.append("")
    if done:
        lines.append(f"COMPLETED ({len(done)})")
        lines += [f"• {p.name} — {_fmt(p.completed_at)}" for p in done[:10]]
        lines.append("")
    if declined:
        lines.append(f"DECLINED ({len(declined)})")
        lines += [f"• {p.name} — {absolute(p.get_absolute_url())}" for p in declined[:10]]
        lines.append("")
    subject = f"{len(runs)} waiting for you" + (f", {len(overdue)} overdue" if overdue else "") if runs else "Your documents update"
    return subject, "\n".join(lines)


def team(ws, since):
    from .models import Packet, StepRun, SupportTicket

    now = timezone.now()
    open_runs = StepRun.objects.filter(packet__workspace=ws, packet__status=Packet.IN_PROGRESS, packet__trashed_at__isnull=True,
                                       packet__is_test=False, status__in=["pending", "opened"])
    total_open, late = open_runs.count(), open_runs.filter(due_at__lt=now)
    by_person = (late.values("assignee_name", "assignee_email", "assignee_group__name").annotate(n=Count("id")).order_by("-n")[:10])
    oldest = open_runs.aggregate(m=Min("created_at"))["m"]
    completed = Packet.objects.filter(workspace=ws, status=Packet.COMPLETED, completed_at__gte=since, is_test=False).count()
    started = Packet.objects.filter(workspace=ws, created_at__gte=since, is_test=False).count()
    tickets = SupportTicket.objects.filter(workspace=ws, status__in=SupportTicket.ACTIVE, severity__in=["high", "critical"])
    if not (total_open or completed or started or tickets.exists()):
        return None
    lines = [f"Team report for {ws.name} — since {_fmt(since)}", ""]
    note = ops_summary(ws, days=max(1, (now - since).days))
    if note["text"]:
        lines += [note["text"], ""]
    lines += [f"Started: {started}   Completed: {completed}   Open steps: {total_open}   Overdue: {late.count()}"]
    lines += [ln for ln in note["facts"][2:]]
    if oldest:
        lines.append(f"Oldest waiting step: {(now - oldest).days} day(s)")
    if by_person:
        lines += ["", "OVERDUE BY PERSON / QUEUE"]
        lines += [f"• {r['assignee_name'] or r['assignee_email'] or r['assignee_group__name'] or '—'}: {r['n']}" for r in by_person]
    if tickets.exists():
        lines += ["", f"OPEN HIGH / CRITICAL TICKETS ({tickets.count()})"]
        lines += [f"• {t.ref} [{t.get_severity_display()}] {t.title}" for t in tickets[:10]]
    lines += ["", f"Team workload: {absolute('/team/workload/')}"]
    return f"Team report: {late.count()} overdue, {completed} completed", "\n".join(lines)


def ops_facts(ws, since):
    """The week in numbers, as plain lines (no field values): volume, overdue work, slow steps, who is overloaded,
    key dates coming up and watchdog flags."""
    import statistics

    from .models import KeyDate, Packet, StepRun

    now = timezone.now()
    open_runs = StepRun.objects.filter(packet__workspace=ws, packet__status=Packet.IN_PROGRESS, packet__trashed_at__isnull=True,
                                       packet__is_test=False, status__in=["pending", "opened"])
    started = Packet.objects.filter(workspace=ws, created_at__gte=since, is_test=False).count()
    completed = Packet.objects.filter(workspace=ws, status=Packet.COMPLETED, completed_at__gte=since, is_test=False).count()
    declined = Packet.objects.filter(workspace=ws, status=Packet.DECLINED, last_activity_at__gte=since, is_test=False).count()
    lines = [f"Period: {_fmt(since)} to {_fmt(now)}",
             f"Started {started}, completed {completed}, declined {declined}; {open_runs.count()} steps open, "
             f"{open_runs.filter(due_at__lt=now).count()} overdue."]
    spans = {}
    for name, wf, a, b in (StepRun.objects.filter(packet__workspace=ws, status="completed", completed_at__gte=since, packet__is_test=False)
                           .values_list("step_name", "packet__workflow__name", "created_at", "completed_at")[:2000]):
        spans.setdefault(f"{wf} › {name}", []).append((b - a).total_seconds() / 3600)
    slow = sorted(((k, statistics.median(v), len(v)) for k, v in spans.items() if len(v) >= 2), key=lambda x: -x[1])[:3]
    if slow:
        lines.append("Slowest steps (median): " + "; ".join(f"{k} {h:.1f} h ({n} done)" for k, h, n in slow))
    load = (open_runs.values("assignee_name", "assignee_email", "assignee_group__name").annotate(n=Count("id")).order_by("-n")[:3])
    if load:
        lines.append("Most open steps: " + "; ".join(f"{r['assignee_name'] or r['assignee_email'] or r['assignee_group__name'] or '—'} {r['n']}" for r in load))
    soon = KeyDate.objects.filter(workspace=ws, done_at__isnull=True, date__gte=now.date(), date__lte=now.date() + dt.timedelta(days=14))
    if soon.exists():
        lines.append(f"Key dates in the next 14 days: {soon.count()} (" + "; ".join(f"{k.label} {k.date:%b %d}" for k in soon[:4]) + ")")
    flagged = sum(1 for f in Packet.objects.filter(workspace=ws, last_activity_at__gte=since).values_list("flags", flat=True)
                  if any(not w.get("dismissed") for w in (f or {}).get("watchdog") or []))
    if flagged:
        lines.append(f"Watchdog flagged {flagged} document(s) as unusual.")
    return lines


OPS_SYSTEM = ("You write the weekly operations note for a manager who runs document workflows. From the facts, write 3 to 5 "
              "short sentences in plain English: what went well, what is slowing down and where, who may need help, and what "
              "is coming up. Use only the facts given; no greetings, no headings, no lists, no invented numbers.")


def ops_summary(ws, days=7, refresh=False):
    """{"text", "facts"} — the AI note is cached for the day and shared by every manager; text is "" without AI."""
    from django.core.cache import cache

    from . import ai_actions

    since = timezone.now() - dt.timedelta(days=days)
    facts = ops_facts(ws, since)
    key = f"ops-summary:{ws.pk}:{days}:{timezone.localdate()}"
    text = "" if refresh else (cache.get(key) or "")
    if not text and ai_actions.enabled(ws):
        try:
            engine = ai_actions.engine_for(ws)
            text = ai_actions.chat(ws, engine, OPS_SYSTEM, "\n".join(facts), json_mode=False, kind="ops_digest").strip()[:1500]
            cache.set(key, text, 6 * 3600)
        except ai_actions.AIError:
            text = ""
    return {"text": text, "facts": facts}


def send_for(m, kind, since, force=False):
    """Send one digest to membership ``m``. Returns True when an email went out."""
    built = personal(m.user, m.workspace, since) if kind == "digest" else team(m.workspace, since)
    if built is None and not force:
        return False
    subject, body = built or ("Nothing new", "Nothing is waiting and nothing changed in this period.")
    mailer.send(m.workspace, m.user.email, "custom", {"subject": f"{m.workspace.name}: {subject}", "message": body})
    return True


def run_due(now=None):
    """Called by the scheduler every tick; sends what's due today (after 07:00 local, weekly ones on Monday)."""
    from accounts.models import Membership

    now = now or timezone.now()
    local = timezone.localtime(now)
    if local.hour < 7:
        return 0
    today, sent = local.date().isoformat(), 0
    for m in Membership.objects.filter(active=True, user__is_active=True).select_related("user", "user__profile", "workspace"):
        prof = getattr(m.user, "profile", None)
        if prof is None:
            continue
        prefs, changed = dict(prof.notification_prefs or {}), False
        for kind in ("digest", "team_report"):
            freq = prefs.get(kind, "off")
            if freq not in ("daily", "weekly") or (kind == "team_report" and not m.can_view_all):
                continue
            if freq == "weekly" and local.weekday() != 0:
                continue
            key = f"{kind}_sent:{m.workspace_id}"
            if prefs.get(key) == today:
                continue
            since = now - dt.timedelta(days=7 if freq == "weekly" else 1)
            if send_for(m, kind, since):
                sent += 1
            prefs[key], changed = today, True
        if changed:
            prof.notification_prefs = prefs
            prof.save(update_fields=["notification_prefs"])
    return sent
