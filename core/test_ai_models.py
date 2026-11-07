"""Model plumbing: backup only when the provider fails, JSON self-repair, the test bench."""
import json
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from accounts.models import Workspace
from flows import ai_actions, ai_eval, ai_providers


class ModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        self.ws = Workspace.objects.get(slug="demo")
        self.ws.settings = {**(self.ws.settings or {}), "ai": {"engine": "ollama:big", "backup_engine": "ollama:small"}}
        self.ws.save()

    def test_backup_only_when_the_provider_errors(self):
        with mock.patch("flows.ai_providers.chat", return_value="ok") as chat:
            ai_actions.chat(self.ws, "ollama:big", "s", "q", json_mode=False, kind="summary")
        self.assertEqual(chat.call_args[0][0], "ollama:big")
        calls = []

        def flaky(engine, *a, **k):
            calls.append(engine)
            if engine == "ollama:big":
                raise ai_providers.EngineError("down")
            return "ok"
        with mock.patch("flows.ai_providers.chat", side_effect=flaky):
            self.assertEqual(ai_actions.chat(self.ws, "ollama:big", "s", "q", json_mode=False, kind="summary"), "ok")
        self.assertEqual(calls, ["ollama:big", "ollama:small"])

        def bad_edit(engine, *a, **k):
            return "not a change"
        with mock.patch("flows.ai_providers.chat", side_effect=bad_edit) as chat:
            ai_actions.chat(self.ws, "ollama:big", "s", "q", json_mode=False, kind="workflow_edit")
        self.assertEqual(chat.call_count, 1)

    def test_json_self_repair(self):
        replies = iter(["sorry, here you go: {broken", '{"a": 1}'])
        with mock.patch("flows.ai_providers.chat", side_effect=lambda *a, **k: next(replies)) as chat:
            self.assertEqual(ai_actions.chat_json(self.ws, "ollama:big", "s", "q", kind="form_build"), {"a": 1})
        self.assertIn("valid JSON", chat.call_args[0][2][-1]["content"])

    def test_benchmark_scores_and_pins_engine(self):
        def answer(engine, system, messages, json_mode=True):
            self.assertEqual(engine, "ollama:test")  # pinned, no fast routing
            q = messages[-1]["content"]
            if "contact form" in q:
                return json.dumps({"fields": [{"type": "full_name", "label": "Name"}, {"type": "email", "label": "Email"},
                                              {"type": "phone", "label": "Phone"}, {"type": "textarea", "label": "Message"}]})
            if "Date of birth" in q:
                return json.dumps({"edits": [{"op": "update", "field": "Phone", "set": {"required": True}},
                                             {"op": "add", "after": "Name", "fields": [{"type": "date", "label": "Date of birth"}]}]})
            if "declined documents" in q:
                return json.dumps({"status": "declined", "from": "2026-10-01"})
            if "passed away" in q:
                return json.dumps({"category": "Claims", "urgency": "high"})
            if "Riley" in q:
                return "Payout of TTD 15,000 approved; finance to release within 5 days."
            return json.dumps({"edits": [{"op": "update_step", "step": "Manager approval", "set": {"subject": "Approval needed"}}]})
        with mock.patch("flows.ai_providers.chat", side_effect=answer):
            r = ai_eval.run(self.ws, "ollama:test")
        self.assertEqual((r["score"], r["passed"]), (100, 6))
        self.ws.refresh_from_db()
        self.assertEqual(self.ws.settings["ai"]["benchmarks"]["ollama:test"]["score"], 100)
        self.assertEqual(ai_actions.engine_for(self.ws), "ollama:big")  # pin released
