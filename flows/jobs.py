"""The background job queue (database-backed: no extra service to run).

Jobs are ``BotJob`` rows: bots with a delay, and bots marked "Run in the background" so a slow AI model or connector
never makes the recipient wait. ``process()`` claims due jobs (``SELECT … FOR UPDATE SKIP LOCKED`` on PostgreSQL, so
several workers can run side by side), runs them, and retries a failed run after 1, 5 and 15 minutes before giving up.
"""
import datetime as dt

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

RETRY_AFTER = (1, 5, 15)  # minutes; attempts = len + 1
LOCK = dt.timedelta(minutes=10)


def _claim(now, limit):
    from .models import BotJob

    with transaction.atomic():
        qs = (BotJob.objects.select_for_update(skip_locked=True)
              .filter(done=False, run_at__lte=now).filter(Q(locked_until__isnull=True) | Q(locked_until__lt=now))
              .order_by("run_at"))
        ids = list(qs.values_list("id", flat=True)[:limit])
        BotJob.objects.filter(id__in=ids).update(locked_until=now + LOCK)
    return ids


def process(now=None, limit=50):
    """Run the jobs that are due. Returns {"ran", "retrying", "failed"}."""
    from .bots import run_bot
    from .models import BotJob, Packet

    now = now or timezone.now()
    out = {"ran": 0, "retrying": 0, "failed": 0}
    for job in BotJob.objects.filter(id__in=_claim(now, limit)).select_related("packet"):
        if job.packet.status not in (Packet.IN_PROGRESS, Packet.DECLINED, Packet.COMPLETED) or job.packet.trashed_at:
            job.done, job.locked_until = True, None
            job.save(update_fields=["done", "locked_until"])
            continue
        log = run_bot(job.packet, job.bot, job.trigger, job.context, from_job=True)
        failed = getattr(log, "status", "") == "failed"
        job.attempts += 1
        job.locked_until = None
        if failed and job.attempts <= len(RETRY_AFTER):
            job.run_at = timezone.now() + dt.timedelta(minutes=RETRY_AFTER[job.attempts - 1])
            job.last_error = (log.message or "")[:500]
            out["retrying"] += 1
        else:
            job.done = True
            job.last_error = (log.message or "")[:500] if failed else ""
            out["failed" if failed else "ran"] += 1
        job.save(update_fields=["attempts", "locked_until", "run_at", "done", "last_error"])
    return out


def enqueue(packet, bot, trigger, context, delay_minutes=0):
    from .models import BotJob

    return BotJob.objects.create(packet=packet, bot=bot, trigger=trigger, context=context or {},
                                 run_at=timezone.now() + dt.timedelta(minutes=delay_minutes))


def run_forever(tick_seconds=30, jobs_seconds=3, report=None):
    """The worker loop: queued jobs every few seconds, the full scheduler (reminders, deadlines, autostarts, key dates,
    webhooks) every ``tick_seconds``. One failing pass never stops the loop."""
    import logging
    import time

    from django.core.cache import cache
    from django.db import close_old_connections

    from .bots import tick

    log = logging.getLogger("paperlane.worker")
    last_tick = 0.0
    while True:
        close_old_connections()
        try:
            if time.monotonic() - last_tick >= tick_seconds:
                last_tick = time.monotonic()
                summary = tick()  # includes process()
                cache.set("scheduler-heartbeat", timezone.now(), 3600)
                if report and any(v for v in summary.values()):
                    report(f"tick: {summary}")
            else:
                done = process()
                if report and any(done.values()):
                    report(f"jobs: {done}")
        except Exception as exc:  # noqa: BLE001 - a DB restart or deploy must not end the worker
            log.exception("worker pass failed: %s", exc)
        finally:
            close_old_connections()
        time.sleep(max(1, min(jobs_seconds, tick_seconds)))
