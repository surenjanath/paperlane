"""Audit trail page: categories, sensitive filter, grouping, presets, chart and CSV columns."""
import datetime as dt

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from accounts.models import Workspace
from flows.models import AuditEvent, Packet

User = get_user_model()


class AuditPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        self.owner = User.objects.get(username="owner")
        self.ws = Workspace.objects.get(slug="demo")
        self.client.force_login(self.owner)
        AuditEvent.objects.filter(workspace=self.ws).delete()
        self.p = Packet.objects.filter(workspace=self.ws).first()
        now = timezone.now()
        mk = lambda action, detail, mins, **kw: AuditEvent.objects.create(  # noqa: E731
            workspace=self.ws, action=action, detail=detail, actor=self.owner, actor_label="Olivia Owner",
            created_at=now - dt.timedelta(minutes=mins), **kw)
        mk("step.sent", "Sent to Manager", 1, packet=self.p)
        mk("step.opened", "Opened Manager", 2, packet=self.p)
        mk("step.completed", "Completed Requester", 3, packet=self.p)
        mk("team.role", "sam@paperlane.test", 30)
        mk("workflow.published", "Purchase v2", 60 * 26, workflow=self.p.workflow)
        mk("packet.trash", "Moved to trash", 60 * 24 * 20, packet=self.p)

    def test_page_categories_grouping_and_sensitive(self):
        r = self.client.get("/audit/")
        self.assertEqual(r.context["stats"]["total"], 5)  # 7 days by default — the 20-day-old event is out
        cats = {k: n for k, _, _, n in r.context["categories"]}
        self.assertEqual((cats["documents"], cats["team"], cats["workflows"]), (3, 1, 1))
        self.assertEqual(r.context["stats"]["sensitive"], 1)  # the role change
        today_rows = list(r.context["days"].values())[0]
        self.assertEqual(len(today_rows[0]["events"]), 3)  # three document events grouped
        self.assertContains(r, "+ 2 more on this document")
        self.assertContains(r, "Role changed")
        self.assertEqual(len(r.context["chart"]), 7)
        self.assertEqual(self.client.get("/audit/?group=0").context["days"][timezone.localdate()][0]["events"].__len__(), 1)
        r = self.client.get("/audit/?cat=team")
        self.assertEqual(r.context["stats"]["total"], 1)
        r = self.client.get("/audit/?range=30&sensitive=1")
        self.assertEqual(r.context["stats"]["total"], 2)  # role change + trash
        r = self.client.get(f"/audit/?workflow={self.p.workflow_id}&range=30")
        self.assertEqual(r.context["stats"]["total"], 5)  # document events of the workflow + the publish

    def test_csv_has_categories_and_targets(self):
        body = self.client.get("/audit/?range=30&format=csv").content.decode()
        self.assertTrue(body.startswith("When,Actor,Category,Action,Event,Detail,Document,Workflow,IP"))
        self.assertIn(",documents,Moved to trash,packet.trash,", body)
        self.assertIn(self.p.name, body)

    def test_auditors_only(self):
        self.client.force_login(User.objects.get(username="fay"))
        self.assertEqual(self.client.get("/audit/").status_code, 302)
        self.client.force_login(User.objects.get(username="ana"))
        self.assertEqual(self.client.get("/audit/").status_code, 200)
