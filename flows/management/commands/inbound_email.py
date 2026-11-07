"""Start a workflow from a raw email on stdin (Postfix pipe, procmail, fetchmail --mda):

    python manage.py inbound_email < message.eml
"""
import sys

from django.core.management.base import BaseCommand

from flows import inbound


class Command(BaseCommand):
    help = "Read one raw email (MIME) from stdin and start the workflow its address belongs to."

    def handle(self, *args, **options):
        raw = sys.stdin.buffer.read()
        try:
            packet = inbound.receive(inbound.parse_mime(raw))
        except inbound.Rejected as exc:
            self.stderr.write(f"Not accepted: {exc}")
            return
        self.stdout.write(f"Started document #{packet.pk}: {packet.name}")
