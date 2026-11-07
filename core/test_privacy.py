"""Sensitive fields: never sent to AI, masked for people who don't need them (page, CSV, PDF), visible to the right people."""
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from accounts.models import Workspace
from core.test_deep import INTEG_BLUEPRINT
from flows import ai_actions, ai_forms, privacy

User = get_user_model()


class PrivacyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        from flows import engine
        from flows.blueprint import build_workflow
        from flows.mcp import Server
        bp = json.loads(json.dumps(INTEG_BLUEPRINT))
        bp["documents"][0]["fields"].append({"id": "acct", "type": "text", "label": "Bank account", "sensitive": True, "filled_by": ["req"]})
        self.owner = User.objects.get(username="owner")
        self.ws = Workspace.objects.get(slug="demo")
        self.ws.settings = {**(self.ws.settings or {}), "ai": {"engine": "ollama:test"}}
        self.ws.save()
        self.wf, _ = build_workflow(self.ws, self.owner, bp, publish=True)
        self.p = engine.start_packet(self.wf, user=self.owner, source="api",
                                     first_assignee={"email": "owner@paperlane.test", "name": "O", "user": self.owner})
        Server(self.ws, self.owner).t_complete_step(self.p.pk, values={"Policy": "POL-7", "Bank account": "000123456789"})
        self.p.refresh_from_db()

    def test_never_sent_to_ai(self):
        facts = ai_actions.document_facts(self.p)
        self.assertIn("POL-7", facts)
        self.assertNotIn("000123456789", facts)
        with mock.patch("flows.ai_providers.chat", return_value="ok") as chat:
            ai_actions.ask(self.p, "what is the account?")
        self.assertNotIn("000123456789", json.dumps(chat.call_args[0]))

    def test_masked_for_others_visible_to_owner(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(f"/documents/{self.p.pk}/?tab=data"), "000123456789")
        ana = User.objects.get(username="ana")  # auditor: can view every document, but isn't involved
        self.client.force_login(ana)
        page = self.client.get(f"/documents/{self.p.pk}/?tab=data")
        self.assertNotContains(page, "000123456789")
        self.assertContains(page, "•••• 6789")
        csv = self.client.get(f"/documents/{self.p.pk}/export/").content.decode()
        self.assertNotIn("000123456789", csv)
        r = self.client.get(f"/documents/{self.p.pk}/download/")
        self.assertIn("masked", r["Content-Disposition"])
        self.assertTrue(privacy.can_see(self.owner, None, self.p))
        self.assertFalse(privacy.can_see(ana, None, self.p))

    def test_ai_builder_marks_sensitive_fields(self):
        fields, _ = ai_forms.normalize([{"type": "text", "label": "Bank account number"}, {"type": "text", "label": "Full name"},
                                        {"type": "text", "label": "Notes", "sensitive": True}], [], "anyone")
        self.assertEqual([bool(f.get("sensitive")) for f in fields], [True, False, True])
