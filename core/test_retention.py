"""Data retention: preview, purge of finished/trashed documents and audit events, legal hold, owner-only rules."""
import datetime as dt

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from accounts.models import Workspace
from flows import retention
from flows.models import AuditEvent, Packet

User = get_user_model()


class RetentionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        self.owner = User.objects.get(username="owner")
        self.ws = Workspace.objects.get(slug="demo")
        self.client.force_login(self.owner)
        old = timezone.now() - dt.timedelta(days=400)
        ps = list(Packet.objects.filter(workspace=self.ws)[:4])
        self.done, self.held, self.trashed, self.fresh = ps
        Packet.objects.filter(pk=self.done.pk).update(status="completed", completed_at=old)
        Packet.objects.filter(pk=self.held.pk).update(status="completed", completed_at=old, flags={"legal_hold": True})
        Packet.objects.filter(pk=self.trashed.pk).update(trashed_at=old)
        Packet.objects.filter(pk=self.fresh.pk).update(status="completed", completed_at=timezone.now())
        AuditEvent.objects.filter(workspace=self.ws).update(created_at=old)

    def test_off_by_default(self):
        self.assertEqual(retention.preview(self.ws), {})
        self.assertEqual(retention.apply(self.ws), {})

    def test_rules_preview_apply_and_legal_hold(self):
        self.client.post("/workspace/retention/", {"documents_days": "365", "statuses": ["completed"], "trash_days": "30", "audit_days": "0"})
        self.ws.refresh_from_db()
        pv = retention.preview(self.ws)
        self.assertGreaterEqual(pv["documents"], 1)
        self.assertGreaterEqual(pv["trash"], 1)
        self.assertNotIn("audit", pv)  # 0 = keep forever
        self.assertContains(self.client.get("/workspace/retention/"), "on legal hold")
        retention.apply(self.ws)
        self.assertFalse(Packet.objects.filter(pk__in=[self.done.pk, self.trashed.pk]).exists())
        self.assertTrue(Packet.objects.filter(pk=self.held.pk).exists())   # legal hold
        self.assertTrue(Packet.objects.filter(pk=self.fresh.pk).exists())  # not old enough
        self.assertTrue(AuditEvent.objects.filter(workspace=self.ws, action="retention.purged").exists())

    def test_audit_pruning_and_hold_toggle(self):
        self.ws.settings = {**(self.ws.settings or {}), "retention": {"audit_days": 90}}
        self.ws.save()
        retention.apply(self.ws)
        self.assertEqual(AuditEvent.objects.filter(workspace=self.ws).exclude(action="retention.purged").count(), 0)
        self.client.post(f"/documents/{self.fresh.pk}/action/hold/")
        self.fresh.refresh_from_db()
        self.assertTrue(self.fresh.flags["legal_hold"])

    def test_only_owners_change_rules(self):
        self.client.force_login(User.objects.get(username="sam"))  # supervisor: can view, not change
        self.assertEqual(self.client.get("/workspace/retention/").status_code, 200)
        self.assertEqual(self.client.post("/workspace/retention/", {"documents_days": "1"}).status_code, 403)
