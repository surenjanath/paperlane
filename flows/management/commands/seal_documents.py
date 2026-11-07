"""Seal completed documents that have no seal yet (e.g. completed before sealing existed).

A backfilled seal proves the record from now on; it can't prove what the record looked like before today, so each one
is recorded in the document's audit trail as a backfill."""
from django.core.management.base import BaseCommand

from flows import engine
from flows.models import Packet
from flows.sealing import seal


class Command(BaseCommand):
    help = "Seal completed documents that don't have a seal yet."

    def add_arguments(self, parser):
        parser.add_argument("--workspace", help="Workspace slug (default: all)")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **o):
        qs = Packet.objects.filter(status=Packet.COMPLETED, seals__isnull=True)
        if o.get("workspace"):
            qs = qs.filter(workspace__slug=o["workspace"])
        n = 0
        for p in qs.select_related("workspace", "workflow"):
            if o["dry_run"]:
                self.stdout.write(f"would seal #{p.pk} {p.name}")
            else:
                s = seal(p)
                engine.audit(p.workspace, "packet.sealed", f"Sealed (backfill — proves the record from {s.sealed_at:%Y-%m-%d}) · code {s.code}", packet=p)
                self.stdout.write(f"sealed #{p.pk} {p.name} → {s.code}")
            n += 1
        self.stdout.write(self.style.SUCCESS(f"{n} document(s) {'to seal' if o['dry_run'] else 'sealed'}."))
