"""Workflow simulator: explores every lane, jump and loop with rolled-back runs; reports problems; keeps and sends nothing."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase

from accounts.models import Workspace

User = get_user_model()
BP = {"name": "Spend with branches", "documents": [{"key": "d", "name": "Request", "fields": [
    {"id": "amt", "type": "currency", "label": "Amount", "required": True, "filled_by": ["req"]},
    {"id": "kind", "type": "dropdown", "label": "Kind", "options": ["Laptop", "Travel"], "filled_by": ["req"]},
    {"id": "dec", "type": "radio", "label": "Manager decision", "options": ["Approve", "Revise"], "required": True, "filled_by": ["mgr"]},
    {"id": "note", "type": "text", "label": "Finance note", "required": True, "readonly": True, "filled_by": ["req"]}]}],
    "steps": [
        {"key": "req", "name": "Requester", "recipient": {"type": "anyone"}},
        {"key": "mgr", "name": "Manager", "recipient": {"type": "email", "email": "sam@paperlane.test"},
         "jumps": [{"to": "req", "when": {"logic": "and", "rules": [{"field": "Manager decision", "op": "eq", "value": "Revise"}]}}]},
        {"key": "route", "type": "branch", "name": "Route by amount", "lanes": [
            {"id": "big", "label": "Over 5,000", "when": {"logic": "and", "rules": [{"field": "Amount", "op": "gt", "value": "5000"},
                                                                                   {"field": "Kind", "op": "eq", "value": "Travel"}]},
             "then": "merge", "steps": [{"key": "fin", "name": "Finance", "recipient": {"type": "group", "group": "Empty finance"}}]},
            {"id": "small", "label": "Up to 5,000", "when": {"logic": "and", "rules": [{"field": "Amount", "op": "lte", "value": "5000"}]},
             "target": "next"}]},
        {"key": "ghost", "name": "Never", "recipient": {"type": "email", "email": "x@paperlane.test"},
         "condition": {"logic": "and", "rules": [{"field": "No such field", "op": "eq", "value": "1"}]}},
        {"key": "done", "name": "Archive", "recipient": {"type": "email", "email": "ana@paperlane.test"}}],
    "bots": [{"type": "webhook", "name": "Ping Slack", "trigger": "flow_complete", "config": {"url": "https://hooks.example.com/x"}}]}


class SimulatorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        from flows.blueprint import build_workflow
        self.owner = User.objects.get(username="owner")
        self.wf, _ = build_workflow(Workspace.objects.get(slug="demo"), self.owner, BP, publish=False)

    def test_explores_and_keeps_nothing(self):
        from flows.models import BotLog, OutboundEmail, Packet, StepRun
        from flows.simulator import routes, simulate
        before = (Packet.objects.count(), StepRun.objects.count(), OutboundEmail.objects.count(), BotLog.objects.count())
        with mock.patch("django.core.mail.EmailMessage.send") as send:
            r = simulate(self.wf, self.owner)
        self.assertEqual(before, (Packet.objects.count(), StepRun.objects.count(), OutboundEmail.objects.count(), BotLog.objects.count()))
        send.assert_not_called()
        paths = {tuple(p["step"] for p in g["path"]) for g in routes(r)}
        self.assertIn(("Requester", "Manager", "Finance", "Archive"), paths)  # compound lane: Amount > 5000 AND Travel
        self.assertIn(("Requester", "Manager", "Archive"), paths)
        self.assertNotIn(("Requester", "Manager", "Requester", "Manager", "Archive"), paths)  # the engine ignores this jump…
        self.assertTrue(all(s["outcome"] == "completed" for s in r["scenarios"]))
        texts = " ".join(p["text"] for p in r["problems"])
        self.assertIn("jump from “Manager” back to “Requester” does nothing", texts)  # …and the simulator says why
        self.assertIn("“No such field”", texts)
        self.assertIn("“Never” is never reached", texts)
        self.assertIn("Empty finance, which has no one", texts)
        self.assertEqual((r["steps_reached"], r["steps_total"]), (4, 5))
        some = r["scenarios"][0]
        self.assertTrue(some["emails"])  # recorded, not sent
        self.assertIn("Ping Slack", {b["name"] for b in some["bots"]})  # outside bot recorded as "would run"
        self.assertIn("Finance note", next(g for g in routes(r) if g["outcome"] == "completed")["required_empty"])

    def test_jump_with_recurrence_loops_once(self):
        import copy

        from flows.blueprint import build_workflow
        from flows.simulator import routes, simulate
        bp = copy.deepcopy(BP)
        bp["name"] = "Spend with recurrence"
        bp["steps"][0]["settings"] = {"recurrence": True}
        half, _ = build_workflow(Workspace.objects.get(slug="demo"), self.owner, {**bp, "name": "Half loop"}, publish=False)
        self.assertIn("“Manager” is skipped the second time", " ".join(p["text"] for p in simulate(half, self.owner)["problems"]))
        bp["steps"][1]["settings"] = {"recurrence": True}
        wf, _ = build_workflow(Workspace.objects.get(slug="demo"), self.owner, bp, publish=False)
        r = simulate(wf, self.owner)
        paths = {tuple(p["step"] for p in g["path"]) for g in routes(r)}
        self.assertIn(("Requester", "Manager", "Requester", "Manager", "Archive"), paths)  # Revise, then the baseline answer
        self.assertNotIn("Recurrence", " ".join(p["text"] for p in r["problems"]))
        self.assertTrue(all(s["outcome"] == "completed" for s in r["scenarios"]))

    def test_page(self):
        from flows import simulator
        self.client.force_login(self.owner)
        url = f"/workflows/{self.wf.pk}/simulate/"
        self.assertContains(self.client.get(url), "Try every branch")
        with mock.patch("flows.simulator.start", return_value=True) as start:
            self.assertRedirects(self.client.post(url), url)
        start.assert_called_once()
        cache.set(simulator._key(self.wf.pk), {"status": "done", "result": simulator.simulate(self.wf, self.owner)}, 60)
        page = self.client.get(url)
        self.assertContains(page, "4 of 5")
        self.assertContains(page, "Finance")
        self.assertEqual(self.client.get(url + "status/").json()["status"], "done")
        self.client.force_login(User.objects.get(username="fay"))  # a filler can't edit the workflow
        self.assertIn(self.client.get(url).status_code, (403, 404))
