from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.utils import timezone

from flows.bots import tick


class Command(BaseCommand):
    help = "Run Paperlane time-based work: queued and delayed bots, reminders, deadlines, scheduled starts, key dates."

    def add_arguments(self, parser):
        parser.add_argument("--loop", type=int, default=0, help="Run the full scheduler every N seconds (0 = run once).")
        parser.add_argument("--jobs-every", type=int, default=3, help="With --loop: check the job queue every N seconds.")

    def handle(self, *args, loop=0, jobs_every=3, **opts):
        if not loop:
            self.stdout.write(f"tick: {tick()}")
            cache.set("scheduler-heartbeat", timezone.now(), 3600)
            return
        from flows.jobs import run_forever

        self.stdout.write(f"worker: scheduler every {loop} s, job queue every {jobs_every} s")
        run_forever(loop, jobs_every, report=self.stdout.write)
