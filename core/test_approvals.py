"""Quick approvals: decision steps listed, one-tap completes through the engine, blocked steps open normally."""
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from accounts.models import Workspace

User = get_user_model()
BP = {"name": "Spend approval", "documents": [{"key": "d", "name": "Request", "fields": [
    {"id": "amt", "type": "currency", "label": "Amount", "required": True, "filled_by": ["req"]},
    {"id": "acct", "type": "text", "label": "Cost centre account", "sensitive": True, "filled_by": ["req"]},
    {"id": "dec", "type": "radio", "label": "Manager decision", "options": ["Approve", "Reject"], "required": True, "filled_by": ["mgr"]},
    {"id": "why", "type": "textarea", "label": "Comments", "filled_by": ["mgr"]}]}],
    "steps": [{"key": "req", "name": "Requester", "recipient": {"type": "anyone"}},
              {"key": "mgr", "name": "Manager", "recipient": {"type": "email", "email": "sam@paperlane.test"}},
              {"key": "fin", "name": "Finance", "recipient": {"type": "email", "email": "ana@paperlane.test"}}]}


class ApprovalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        from flows import engine
        from flows.blueprint import build_workflow
        from flows.mcp import Server
        owner = User.objects.get(username="owner")
        ws = Workspace.objects.get(slug="demo")
        wf, _ = build_workflow(ws, owner, BP, publish=True)
        self.p = engine.start_packet(wf, user=owner, source="api", first_assignee={"email": "owner@paperlane.test", "name": "O", "user": owner})
        Server(ws, owner).t_complete_step(self.p.pk, values={"Amount": "2500", "Cost centre account": "9988776655"})
        self.p.refresh_from_db()
        self.sam = User.objects.get(username="sam")
        self.client.force_login(self.sam)

    def test_one_tap_approve(self):
        page = self.client.get("/approvals/")
        card = next(c for c in page.context["cards"] if c["run"].packet_id == self.p.pk)
        self.assertEqual([o for o, _ in card["options"]], ["Approve", "Reject"])
        self.assertIn(("Amount", "2500"), card["plan"]["facts"])
        self.assertIn(("Cost centre account", "••••"), card["plan"]["facts"])  # sensitive stays hidden
        r = self.client.post(f"/approvals/{card['run'].token}/", {"decision": "Approve", "comment": "OK by me"})
        self.assertEqual(r.status_code, 302)
        self.p.refresh_from_db()
        vals = self.p.flat_values()
        self.assertEqual((vals["Manager decision"], vals["Comments"]), ("Approve", "OK by me"))
        self.assertEqual(self.p.runs.get(step_key=card["run"].step_key).status, "completed")
        self.assertTrue(self.p.runs.filter(step_name="Finance", status__in=["pending", "opened"]).exists())
        self.assertEqual(self.p.field_changes.filter(label="Manager decision").first().actor, self.sam)

    def test_guards(self):
        card = next(c for c in self.client.get("/approvals/").context["cards"] if c["run"].packet_id == self.p.pk)
        self.client.post(f"/approvals/{card['run'].token}/", {"decision": "Maybe"})  # not an option
        self.assertEqual(self.p.runs.get(pk=card["run"].pk).status, "pending")
        self.client.force_login(User.objects.get(username="fay"))  # not the assignee
        self.assertEqual(self.client.post(f"/approvals/{card['run'].token}/", {"decision": "Approve"}).status_code, 403)


class DecisionToneTests(TestCase):
    def test_negatives_win(self):
        from flows.views.approvals import _tone

        for opt, tone in [("Approve", "good"), ("Approved", "good"), ("Yes", "good"), ("Accept", "good"),
                          ("Not approved", "bad"), ("Disapprove", "bad"), ("No", "bad"), ("Reject", "bad"),
                          ("Declined", "bad"), ("Do not authorise", "bad"), ("Send back", "neutral"), ("Reduce", "neutral")]:
            self.assertEqual(_tone(opt), tone, opt)
