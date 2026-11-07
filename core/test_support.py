"""“What went wrong?”: rule-based diagnosis, severity/impact, automatic tickets (deduplicated), the assistant flow and the
ticket pages. The model is mocked."""
import datetime as dt
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from accounts.models import Workspace
from core.test_deep import INTEG_BLUEPRINT

User = get_user_model()


class DiagnosisTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        from flows.blueprint import build_workflow
        self.owner = User.objects.get(username="owner")
        self.ws = Workspace.objects.get(slug="demo")
        self.ws.settings = {**(self.ws.settings or {}), "ai": {"engine": "ollama:test"}}
        self.ws.save()
        self.wf, _ = build_workflow(self.ws, self.owner, INTEG_BLUEPRINT, publish=True)
        self.client.force_login(self.owner)

    def start(self, policy="POL-1"):
        from flows import engine
        from flows.mcp import Server
        p = engine.start_packet(self.wf, user=self.owner, source="api",
                                first_assignee={"email": "owner@paperlane.test", "name": "Owner", "user": self.owner})
        Server(self.ws, self.owner).t_complete_step(p.pk, values={"Policy": policy})
        p.refresh_from_db()
        return p

    def fail_bot(self, p, key="b1", name="Prefill email"):
        from flows.models import BotLog
        return BotLog.objects.create(workspace=self.ws, workflow=self.wf, packet=p, bot_key=key, bot_name=name,
                                     trigger="step_complete", status="failed", message="RuntimeError: Nothing to pre-fill")

    def test_healthy_document(self):
        from flows import diagnose as dg
        d = dg.diagnose(self.start())
        self.assertEqual((d["severity"], d["findings"]), ("none", []))

    def test_bot_failure_severity_grows_with_impact_and_success_clears_it(self):
        from flows import diagnose as dg
        from flows.models import BotLog
        p = self.start()
        self.fail_bot(p)
        d = dg.diagnose(p)
        self.assertEqual(d["severity"], "medium")
        self.assertEqual(d["findings"][0]["code"], "bot_failed")
        for _ in range(2):
            self.fail_bot(self.start())
        d = dg.diagnose(p)
        self.assertEqual((d["severity"], d["affected"]), ("high", 3))
        self.assertIn("3 documents", d["impact"])
        # a later "conditions not met" run doesn't hide it; a later success does
        BotLog.objects.create(workspace=self.ws, workflow=self.wf, packet=p, bot_key="b1", bot_name="Prefill email", status="skipped",
                              message="Conditions not met")
        self.assertTrue(dg.diagnose(p)["findings"])
        BotLog.objects.create(workspace=self.ws, workflow=self.wf, packet=p, bot_key="b1", bot_name="Prefill email", status="success",
                              message="ok")
        self.assertFalse([f for f in dg.diagnose(p)["findings"] if f["code"] == "bot_failed"])

    def test_stuck_missing_recipient_and_overdue(self):
        from flows import diagnose as dg
        from flows.models import StepRun
        p = self.start()
        run = p.runs.get(status__in=["pending", "opened"])
        StepRun.objects.filter(pk=run.pk).update(assignee_email="", assignee_user=None, assignee_group=None,
                                                 due_at=timezone.now() - dt.timedelta(days=4))
        codes = {f["code"]: f["severity"] for f in dg.diagnose(p)["findings"]}
        self.assertEqual(codes["no_recipient"], "high")
        self.assertEqual(codes["overdue"], "high")
        StepRun.objects.filter(pk=run.pk).update(status="expired", expires_at=timezone.now())
        d = dg.diagnose(p)
        self.assertEqual(d["findings"][0]["code"], "expired")
        self.assertIn("blocked", d["impact"])
        StepRun.objects.filter(pk=run.pk).update(status="completed")
        d = dg.diagnose(p)
        self.assertEqual((d["findings"][0]["code"], d["severity"]), ("stalled", "high"))

    def test_auto_ticket_once_per_problem_and_notifies_owners(self):
        from core.models import Notification
        from flows import diagnose as dg
        from flows.models import SupportTicket
        docs = [self.start() for _ in range(3)]
        for p in docs:
            self.fail_bot(p)
        with mock.patch("flows.ai_providers.chat", return_value="The prefill bot failed."):
            r1 = dg.run(docs[0], self.owner)
            r2 = dg.run(docs[1], self.owner)
        self.assertEqual(r1["severity"], "high")
        self.assertTrue(r1["ticket"]["created"] and r1["ticket"]["auto"])
        self.assertFalse(r2["ticket"]["created"])
        t = SupportTicket.objects.get()
        self.assertEqual((t.severity, t.source, t.affected_count, t.summary), ("high", "auto", 3, "The prefill bot failed."))
        self.assertIn("seen again on document", t.notes[-1]["text"])
        self.assertTrue(Notification.objects.filter(kind="ticket", user__username__in=["owner", "sam"]).count() >= 2)
        # more documents → raised to critical on the same ticket
        for _ in range(8):
            self.fail_bot(self.start())
        with mock.patch("flows.ai_providers.chat", return_value="x"):
            dg.run(docs[0], self.owner)
        t.refresh_from_db()
        self.assertEqual((t.severity, SupportTicket.objects.count()), ("critical", 1))
        # resolved, then seen again within a week → the same ticket is reopened
        t.status, t.resolved_at = "resolved", timezone.now()
        t.save()
        with mock.patch("flows.ai_providers.chat", return_value="x"):
            r = dg.run(docs[2], self.owner)
        t.refresh_from_db()
        self.assertEqual((t.status, SupportTicket.objects.count(), r["ticket"]["ref"]), ("open", 1, t.ref))
        self.assertTrue(any("Reopened" in n["text"] for n in t.notes))
        # medium problems are not filed automatically
        other = self.start()
        self.fail_bot(other, key="b2", name="Rename")
        with mock.patch("flows.ai_providers.chat", return_value="x"):
            self.assertIsNone(dg.run(other, self.owner)["ticket"])

    def test_assistant_diagnoses_document_page_and_manual_ticket(self):
        from flows.models import SupportTicket
        p = self.start()
        self.fail_bot(p)
        page = self.client.get(f"/documents/{p.pk}/")
        self.assertContains(page, "What went wrong?")
        self.assertContains(page, 'id="hc-chat"')  # the assistant is on every app page
        with mock.patch("flows.ai_providers.chat", return_value="**Prefill email** failed because the field was empty.") as chat:
            j = self.client.post("/help/ask/", {"question": "why did this fail?", "path": f"/documents/{p.pk}/"}).json()
        self.assertIn("Prefill email", chat.call_args[0][2][0]["content"])
        d = j["diagnosis"]
        self.assertEqual((d["packet"], d["severity"]), (p.pk, "medium"))
        self.assertIsNone(d["ticket"])
        r = self.client.post(f"/documents/{p.pk}/ticket/", {"note": "Customer is waiting"}).json()
        self.assertTrue(r["ticket"]["created"])
        t = SupportTicket.objects.get()
        self.assertEqual((t.source, t.severity, t.notes[0]["text"]), ("assistant", "medium", "Customer is waiting"))
        # "#id" from any page; general questions still use the articles
        with mock.patch("flows.ai_providers.chat", return_value="ok"):
            j = self.client.post("/help/ask/", {"question": f"what went wrong with #{p.pk}?", "path": "/inbox/"}).json()
            self.assertEqual(j["diagnosis"]["packet"], p.pk)
            other = self.start()
            j = self.client.post("/help/ask/", {"question": f"document #{other.pk} failed too, why?", "path": f"/documents/{p.pk}/"}).json()
            self.assertEqual(j["diagnosis"]["packet"], other.pk)  # the typed number wins over the page
            j = self.client.post("/help/ask/", {"question": "how do I add reminders?", "path": f"/documents/{p.pk}/"}).json()
        self.assertNotIn("diagnosis", j)

    def test_ticket_pages_and_permissions(self):
        from flows import diagnose as dg
        p = self.start()
        self.fail_bot(p)
        t, _ = dg.file_ticket(p, dg.diagnose(p), self.owner, source="manual", note="help")
        self.assertContains(self.client.get("/support/"), t.title)
        self.assertContains(self.client.get(t.get_absolute_url()), "Findings")
        self.client.post(t.get_absolute_url(), {"do": "note", "text": "Fixed the field mapping"})
        self.client.post(t.get_absolute_url(), {"do": "status", "status": "resolved"})
        t.refresh_from_db()
        self.assertEqual(t.status, "resolved")
        self.assertIsNotNone(t.resolved_at)
        self.assertEqual(t.notes[1]["text"], "Fixed the field mapping")
        fay = User.objects.get(username="fay")  # a filler who can't see this document
        self.client.force_login(fay)
        self.assertNotContains(self.client.get("/support/?status="), t.title)
        self.assertEqual(self.client.get(t.get_absolute_url()).status_code, 404)
        # platform support queue
        self.owner.is_superuser = True
        self.owner.save()
        self.client.force_login(self.owner)
        self.assertContains(self.client.get("/platform/tickets/?status="), t.ref)
        self.client.post("/platform/tickets/?status=", {"ticket": t.pk, "status": "in_progress", "note": "Looking into it"})
        t.refresh_from_db()
        self.assertEqual(t.status, "in_progress")
        self.assertTrue(t.notes[-1]["staff"])


class TicketRoutingTests(DiagnosisTests):
    """Response targets, support alerts, assignment and first response."""

    def test_targets_alerts_assignment(self):
        from django.core import mail

        from flows import diagnose as dg
        from flows.models import SupportTicket
        from superadmin.models import get_settings

        ps = get_settings()
        ps.support_email = "support@platform.test"
        ps.save()
        from django.core.cache import cache
        cache.clear()
        docs = [self.start() for _ in range(3)]
        for p in docs:
            self.fail_bot(p)
        mail.outbox.clear()
        with mock.patch("flows.ai_providers.chat", return_value="x"):
            dg.run(docs[0], self.owner)
            dg.run(docs[1], self.owner)  # same problem: no second alert
        t = SupportTicket.objects.get()
        self.assertAlmostEqual((t.due_at - t.created_at).total_seconds(), 24 * 3600, delta=5)  # High → 1 day
        alerts = [m for m in mail.outbox if "support@platform.test" in m.to]
        self.assertEqual(len(alerts), 1)
        self.assertIn(t.ref, alerts[0].subject)
        # escalation to critical tightens the target and alerts again
        for _ in range(8):
            self.fail_bot(self.start())
        with mock.patch("flows.ai_providers.chat", return_value="x"):
            dg.run(docs[0], self.owner)
        t.refresh_from_db()
        self.assertEqual(t.severity, "critical")
        self.assertAlmostEqual((t.due_at - t.created_at).total_seconds(), 4 * 3600, delta=5)
        self.assertEqual(len([m for m in mail.outbox if "support@platform.test" in m.to]), 2)
        # staff assign + reply → first response recorded
        self.owner.is_superuser = True
        self.owner.save()
        self.client.post("/platform/tickets/?status=", {"ticket": t.pk, "status": "in_progress", "assignee": self.owner.pk, "note": "On it"})
        t.refresh_from_db()
        self.assertEqual(t.assignee, self.owner)
        self.assertIsNotNone(t.first_response_at)
        self.assertFalse(t.response_overdue)
