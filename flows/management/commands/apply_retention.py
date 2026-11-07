"""Apply each workspace's data-retention rules now (the scheduler also runs them daily)."""
from django.core.management.base import BaseCommand

from accounts.models import Workspace
from flows import retention


class Command(BaseCommand):
    help = "Apply data-retention rules now."

    def add_arguments(self, parser):
        parser.add_argument("--workspace")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **o):
        qs = Workspace.objects.all()
        if o.get("workspace"):
            qs = qs.filter(slug=o["workspace"])
        for ws in qs:
            r = retention.apply(ws, dry_run=o["dry_run"])
            if r:
                self.stdout.write(f"{ws.slug}: {'would delete' if o['dry_run'] else 'deleted'} {r}")
