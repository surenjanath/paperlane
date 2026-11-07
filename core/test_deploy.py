import os
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.test import TestCase

from accounts.models import Membership, Workspace


def run(*args, password=""):
    out = StringIO()
    with mock.patch.dict(os.environ, {"PAPERLANE_ADMIN_PASSWORD": password}):
        call_command("bootstrap", *args, stdout=out)
    return out.getvalue()


class BootstrapTests(TestCase):
    def test_creates_super_admin_and_first_workspace(self):
        out = run("--email", "Admin@Example.com", "--workspace", "Acme", password="a-long-enough-pass")
        user = get_user_model().objects.get(email="admin@example.com")
        self.assertTrue(user.is_superuser and user.is_staff and user.check_password("a-long-enough-pass"))
        ws = Workspace.objects.get(name="Acme")
        self.assertTrue(Membership.objects.filter(user=user, workspace=ws, role=Membership.OWNER).exists())
        self.assertIn("Created super admin", out)

    def test_rerun_keeps_password_and_adds_no_workspace(self):
        run("--email", "admin@example.com", "--workspace", "Acme", password="a-long-enough-pass")
        out = run("--email", "admin@example.com", "--workspace", "Other", password="another-long-pass")
        self.assertIn("already exists", out)
        self.assertTrue(get_user_model().objects.get(email="admin@example.com").check_password("a-long-enough-pass"))
        self.assertFalse(Workspace.objects.filter(name="Other").exists())

    def test_reset_password_and_short_password_refused(self):
        with self.assertRaises(CommandError):
            run("--email", "admin@example.com", password="short")
        run("--email", "admin@example.com", password="a-long-enough-pass")
        run("--email", "admin@example.com", "--reset-password", password="a-new-long-password")
        self.assertTrue(get_user_model().objects.get(email="admin@example.com").check_password("a-new-long-password"))


class WorkerHeartbeatTests(TestCase):
    def test_tick_command_records_heartbeat(self):
        cache.delete("scheduler-heartbeat")
        call_command("paperlane_tick", stdout=StringIO())
        self.assertIsNotNone(cache.get("scheduler-heartbeat"))
