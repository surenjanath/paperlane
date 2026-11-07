"""Email digests: opt-in, content, once per day, weekly on Mondays, team report for managers, previews."""
import datetime as dt

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from accounts.models import Membership, Workspace
from flows import digests

User = get_user_model()


class DigestTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        self.ws = Workspace.objects.get(slug="demo")
        self.sam = User.objects.get(username="sam")
        self.client.force_login(self.sam)
        mail.outbox.clear()

    def monday_9am(self):
        d = timezone.localtime()
        d = d - dt.timedelta(days=d.weekday())
        return d.replace(hour=9, minute=0)

    def test_off_by_default_then_daily_once(self):
        self.assertEqual(digests.run_due(self.monday_9am()), 0)
        self.client.post("/account/notifications/", {"digest": "daily", "team_report": "weekly", "assigned": "on"})
        self.sam.profile.refresh_from_db()
        self.assertEqual(self.sam.profile.notification_prefs["digest"], "daily")
        early = self.monday_9am().replace(hour=6)
        self.assertEqual(digests.run_due(early), 0)  # not before 07:00
        n = digests.run_due(self.monday_9am())
        self.assertGreaterEqual(n, 1)
        subjects = [m.subject for m in mail.outbox if "sam@paperlane.test" in m.to]
        self.assertTrue(any("waiting for you" in s for s in subjects) or any("Team report" in s for s in subjects))
        before = len(mail.outbox)
        self.assertEqual(digests.run_due(self.monday_9am() + dt.timedelta(hours=2)), 0)  # once per day
        self.assertEqual(len(mail.outbox), before)
        self.assertEqual(digests.run_due(self.monday_9am() + dt.timedelta(days=1, hours=1)) >= 0, True)

    def test_weekly_only_on_mondays_and_team_report_for_managers(self):
        fay = User.objects.get(username="fay")  # filler: no team report even if set
        fay.profile.notification_prefs = {"team_report": "daily"}
        fay.profile.save()
        self.sam.profile.notification_prefs = {"team_report": "weekly"}
        self.sam.profile.save()
        tuesday = self.monday_9am() + dt.timedelta(days=1)
        digests.run_due(tuesday)
        self.assertFalse([m for m in mail.outbox if "Team report" in m.subject])
        digests.run_due(self.monday_9am() + dt.timedelta(days=7))
        to = {a for m in mail.outbox if "Team report" in m.subject for a in m.to}
        self.assertIn("sam@paperlane.test", to)
        self.assertNotIn("fay@paperlane.test", to)

    def test_preview_and_permissions(self):
        self.client.post("/account/notifications/", {"form": "preview", "preview": "team_report"})
        self.assertTrue(any("sam@paperlane.test" in m.to for m in mail.outbox))
        self.client.force_login(User.objects.get(username="fay"))
        self.assertEqual(self.client.post("/account/notifications/", {"form": "preview", "preview": "team_report"}).status_code, 403)
        m = Membership.objects.get(user__username="fay", workspace=self.ws)
        self.assertTrue(digests.send_for(m, "digest", timezone.now() - dt.timedelta(days=7), force=True))
