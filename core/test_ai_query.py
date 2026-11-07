"""Ask AI across documents: the model only plans; Paperlane filters with the user's permissions and does the maths."""
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from accounts.models import Membership, Workspace
from core.test_deep import INTEG_BLUEPRINT
from flows import ai_query

User = get_user_model()


class AskAcrossTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        from flows import engine
        from flows.blueprint import build_workflow
        from flows.mcp import Server
        self.owner = User.objects.get(username="owner")
        self.ws = Workspace.objects.get(slug="demo")
        self.ws.settings = {**(self.ws.settings or {}), "ai": {"engine": "ollama:test"}}
        self.ws.save()
        self.wf, _ = build_workflow(self.ws, self.owner, {**INTEG_BLUEPRINT, "name": "Claims intake"}, publish=True)
        for pol, fund in [("A1", "500"), ("A2", "15000"), ("A3", "12000")]:
            p = engine.start_packet(self.wf, user=self.owner, source="api",
                                    first_assignee={"email": "owner@paperlane.test", "name": "O", "user": self.owner})
            Server(self.ws, self.owner).t_complete_step(p.pk, values={"Policy": pol, "Fund": fund})
        self.m = Membership.objects.get(user=self.owner, workspace=self.ws)
        self.client.force_login(self.owner)

    def ask(self, plan):
        return mock.patch("flows.ai_providers.chat", return_value=json.dumps(plan))

    def test_filters_counts_and_sums(self):
        plan = {"workflow": "claims intake", "status": "in_progress", "step": "Review",
                "fields": [{"field": "Fund", "op": "gt", "value": "10000"}], "aggregate": "list"}
        with self.ask(plan) as chat:
            r = ai_query.answer(self.m, "Which claims over 10,000 are waiting on review?")
        self.assertIn("Claims intake", chat.call_args[0][2][0]["content"])  # the schema, not document data
        self.assertNotIn("A2", chat.call_args[0][2][0]["content"])
        self.assertEqual(r["query"]["count"], 2)
        self.assertEqual({row["values"]["Fund"] for row in r["query"]["rows"]}, {"15000", "12000"})
        with self.ask({"workflow": "Claims intake", "aggregate": "sum", "of": "Fund", "fields": []}):
            r = ai_query.answer(self.m, "Total fund of claims intake?")
        self.assertEqual(r["query"]["value"], 27500)
        with self.ask({"aggregate": "none"}):
            self.assertIsNone(ai_query.answer(self.m, "hello"))

    def test_respects_permissions(self):
        fay = Membership.objects.get(user__username="fay", workspace=self.ws)  # filler: doesn't see these documents
        with self.ask({"workflow": "Claims intake", "aggregate": "count", "fields": []}):
            r = ai_query.answer(fay, "how many claims intake documents?")
        self.assertEqual(r["query"]["count"], 0)

    def test_chat_routes_data_questions(self):
        with self.ask({"workflow": "Claims intake", "aggregate": "count", "fields": []}):
            j = self.client.post("/help/ask/", {"question": "How many claims intake documents are there?", "path": "/inbox/"}).json()
        self.assertEqual(j["query"]["count"], 3)
        with mock.patch("flows.ai_providers.chat", return_value="Open the builder…") as chat:
            j = self.client.post("/help/ask/", {"question": "How do I list documents by tag?", "path": "/inbox/"}).json()
        self.assertNotIn("query", j)  # how-to questions still go to the help articles
