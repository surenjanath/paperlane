import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from accounts.models import Workspace
from flows import services
from flows.importers import import_any


class Command(BaseCommand):
    help = "Import a Paperlane export or a workflow blueprint as a Paperlane workflow."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--workspace", required=True, help="Workspace slug")
        parser.add_argument("--user", required=True, help="Username or email of the owner")
        parser.add_argument("--publish", action="store_true")

    def handle(self, path, workspace, user, publish=False, **opts):
        ws = Workspace.objects.filter(slug=workspace).first() or self._fail(f"No workspace {workspace!r}")
        User = get_user_model()
        owner = User.objects.filter(username=user).first() or User.objects.filter(email__iexact=user).first() or self._fail("No such user")
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        wf, report = import_any(data, ws, owner)
        if publish:
            services.publish(wf, owner)
        self.stdout.write(self.style.SUCCESS(f"Imported “{wf.name}” (id {wf.pk})"))
        for section in ("documents", "steps", "groups", "bots", "warnings"):
            if report.get(section):
                self.stdout.write(f"\n{section.upper()} ({len(report[section])})")
                for line in report[section]:
                    self.stdout.write(f"  - {line}")

    def _fail(self, msg):
        raise CommandError(msg)
