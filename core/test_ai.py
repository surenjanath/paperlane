"""AI features: usage log, Ask AI follow-ups, summary cache, fill from text, review/translate bots, AI search and the
builder's AI assistant. The model is always mocked (flows.ai_providers.chat)."""
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from accounts.models import Workspace
from core.test_deep import INTEG_BLUEPRINT

User = get_user_model()


def fake(reply):
    return mock.patch("flows.ai_providers.chat", side_effect=reply if callable(reply) else None,
                      return_value=None if callable(reply) else reply)


class AIFeatureTests(TestCase):
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
        self.req = self.wf.ordered_steps()[0].key
        self.client.force_login(self.owner)

    def start(self, values=None):
        from flows import engine
        from flows.mcp import Server
        p = engine.start_packet(self.wf, user=self.owner, source="api",
                                first_assignee={"email": "owner@paperlane.test", "name": "Owner", "user": self.owner})
        if values is not None:
            Server(self.ws, self.owner).t_complete_step(p.pk, values=values)
        p.refresh_from_db()
        return p

    def test_ask_follow_up_sends_history_and_logs_usage(self):
        from flows.models import AIUsage
        p = self.start({"Policy": "POL-1"})
        with fake("It was approved.") as chat:
            r = self.client.post(f"/documents/{p.pk}/ask-ai/", {
                "question": "And then?", "history": json.dumps([{"q": "Which policy?", "a": "POL-1"}])})
        self.assertEqual(r.json()["answer"], "It was approved.")
        msgs = chat.call_args[0][2]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        self.assertIn("POL-1", chat.call_args[0][1])  # document data is in the system prompt
        u = AIUsage.objects.get(kind="ask")
        self.assertTrue(u.ok)
        self.assertEqual((u.packet_id, u.user_id, u.chars_out), (p.pk, self.owner.pk, len("It was approved.")))
        with mock.patch("flows.ai_providers.chat", side_effect=__import__("flows.ai_providers", fromlist=["x"]).EngineError("down")):
            self.assertEqual(self.client.post(f"/documents/{p.pk}/ask-ai/", {"question": "x"}).status_code, 400)
        self.assertFalse(AIUsage.objects.filter(ok=False).get().ok)
        page = self.client.get("/workspace/ai/")
        self.assertContains(page, "Usage — last 7 days")
        failed = AIUsage.objects.filter(ok=False).get()
        self.assertContains(page, f"/workspace/ai/usage/{failed.pk}/")
        self.assertNotContains(page, "down")
        panel = self.client.get(f"/workspace/ai/usage/{failed.pk}/")
        self.assertContains(panel, "down")
        self.assertContains(panel, "Ask AI")
        other = Workspace.objects.create(name="Other", slug="other-usage")
        foreign = AIUsage.objects.create(workspace=other, kind="ask", engine="x", error="hidden-failure")
        self.assertEqual(self.client.get(f"/workspace/ai/usage/{foreign.pk}/").status_code, 404)

    def test_summary_is_cached_until_data_changes(self):
        p = self.start({"Policy": "POL-1"})
        self.assertContains(self.client.get(f"/documents/{p.pk}/"), "AI summary")
        with fake("Short summary.") as chat:
            r1 = self.client.post(f"/documents/{p.pk}/ai-summary/")
            r2 = self.client.post(f"/documents/{p.pk}/ai-summary/")
        self.assertEqual(r1.json()["text"], "Short summary.")
        self.assertEqual(r2.json()["text"], "Short summary.")
        self.assertEqual(chat.call_count, 1)
        self.assertContains(self.client.get(f"/documents/{p.pk}/"), "Short summary.")
        with fake("New.") as chat:
            self.assertEqual(self.client.post(f"/documents/{p.pk}/ai-summary/", {"refresh": "1"}).json()["text"], "New.")

    def test_fill_from_text_returns_only_editable_fields(self):
        p = self.start()
        run = p.runs.get(step_key=self.req)
        page = self.client.get(f"/f/{run.token}/")
        self.assertContains(page, "Fill from text")
        reply = '{"Policy": "POL-77", "Priority": "high", "Fund": "$5,000"}'
        with fake(reply) as chat:
            r = self.client.post(f"/f/{run.token}/ai-extract/", {"text": "Hello, my policy number is POL-77, urgent."})
        vals = r.json()["values"]
        self.assertIn("POL-77", vals.values())
        self.assertTrue(all(k.startswith("f_") for k in vals))
        self.assertIn("POL-77", chat.call_args[0][2][0]["content"])
        # Policy is filled by the requester only; the reviewer can't have it filled
        p.refresh_from_db()
        self.assertEqual(self.client.post(f"/f/{run.token}/ai-extract/", {"text": "short"}).status_code, 400)
        self.ws.settings["ai"]["fill_assist"] = False
        self.ws.save()
        self.assertEqual(self.client.post(f"/f/{run.token}/ai-extract/", {"text": "x" * 20}).status_code, 403)
        self.assertNotContains(self.client.get(f"/f/{run.token}/"), "Fill from text")

    def test_review_and_translate_bots(self):
        from flows import services
        from flows.models import Bot
        Bot.objects.create(workflow=self.wf, name="Check", bot_type="ai_review", trigger=Bot.STEP_COMPLETE, step_key=self.req,
                           config={"rules": "Fund must be under 1000", "target": "Summary", "tag": "#Needs review",
                                   "add_comment": True})
        Bot.objects.create(workflow=self.wf, name="Tr", bot_type="ai_translate", trigger=Bot.STEP_COMPLETE, step_key=self.req,
                           config={"mapping": "Policy = Holder", "language": "Spanish"})
        services.publish(self.wf, self.owner)

        def reply(engine, system, messages, json_mode=True):
            return ('{"passed": false, "issues": ["Fund is 5000"]}' if "check a business document" in system
                    else '{"Policy": "Póliza POL"}')
        with fake(reply):
            p = self.start({"Policy": "Policy POL", "Fund": "5000"})
        vals = p.flat_values()
        self.assertEqual(vals["Summary"], "Needs review")
        self.assertEqual(vals["Holder"], "Póliza POL")
        self.assertIn("#Needs review", list(p.tags.values_list("name", flat=True)))
        self.assertIn("Fund is 5000", p.comments.get(author_label="AI review").text)

    def test_ai_search_turns_words_into_filters(self):
        self.start({"Policy": "X"})
        with fake(json.dumps({"status": "in_progress", "workflow": "integration TEST", "from": "2026-01-01", "tag": "nope",
                              "quick": "bogus"})):
            r = self.client.get("/documents/ai-search/", {"q": "open integration docs this year"})
        url = r["Location"]
        self.assertIn("status=in_progress", url)
        self.assertIn(f"workflow={self.wf.pk}", url)
        self.assertIn("from=2026-01-01", url)
        self.assertNotIn("tag=", url)
        self.assertNotIn("quick=", url)
        page = self.client.get(url)
        self.assertContains(page, "Results for")
        self.assertContains(page, "Integration test")
        # model unreachable → plain text search
        with mock.patch("flows.ai_providers.chat", side_effect=__import__("flows.ai_providers", fromlist=["x"]).EngineError("down")):
            r = self.client.get("/documents/ai-search/", {"q": "POL"})
        self.assertIn("q=POL", r["Location"])

    def test_builder_assistant_panel_compare_apply_undo(self):
        from flows import ai_workflow
        from flows.models import Packet
        url, F = f"/workflows/{self.wf.pk}/ai/", {"HTTP_X_REQUESTED_WITH": "fetch"}
        original_keys = [s.key for s in self.wf.ordered_steps()]
        builder = self.client.get(f"/workflows/{self.wf.pk}/builder/")
        self.assertContains(builder, 'id="ai-panel"')
        self.assertContains(builder, "data-ai-before")
        self.assertContains(builder, f'data-step="{original_keys[0]}"')
        self.assertRedirects(self.client.get(url), f"/workflows/{self.wf.pk}/builder/?ai=1", fetch_redirect_response=False)
        self.assertContains(self.client.get(url, **F), "What should change?")
        with fake('{"suggestions": [{"title": "Require the fund", "why": "It drives routing", "request": "Make Fund required"}]}') as chat:
            r = self.client.post(url, {"do": "suggest"}, **F)
        self.assertContains(r, "Require the fund")
        self.assertIn("Requester", chat.call_args[0][2][0]["content"])  # the model sees the current workflow
        self.assertEqual(self.client.get(url + "after/").status_code, 404)  # nothing proposed yet
        bp = ai_workflow.current_blueprint(self.wf)
        bp["documents"][0]["fields"].append({"id": "iban", "type": "text", "label": "IBAN", "filled_by": [original_keys[0]]})
        bp["steps"].append({"key": "fin", "name": "Finance", "recipient": {"type": "email", "email": "fin@paperlane.test"}})
        bp["steps"][1]["name"] = "Senior review"
        bp["changes"] = ["Added IBAN", "Added a Finance step"]
        with fake(json.dumps(bp)) as chat:
            r = self.client.post(url, {"do": "plan", "text": "Add IBAN and a finance step"}, **F)
        self.assertIn("Add IBAN and a finance step", chat.call_args[0][2][0]["content"])
        self.assertContains(r, "data-ai-plan")
        self.assertContains(r, "Added a Finance step")
        self.assertContains(r, f'data-goto="{original_keys[1]}"')
        # the "after" canvas: rendered from the proposal, nothing saved
        after = self.client.get(url + "after/").json()
        self.assertIn("Finance", after["html"])
        self.assertIn("is-preview", after["html"])
        self.assertEqual(sorted(after["marks"].values()), ["added", "changed"])
        self.assertEqual(set(after["keys"]), {*original_keys, "fin"})
        self.assertEqual([s.key for s in self.wf.ordered_steps()], original_keys)
        self.assertFalse(self.wf.steps.filter(name="Finance").exists())
        # panel reload keeps the proposal; discard drops it
        self.assertContains(self.client.get(url, **F), "data-ai-plan")
        self.assertNotContains(self.client.post(url, {"do": "discard"}, **F), "data-ai-plan")
        with fake(json.dumps(bp)):
            self.client.post(url, {"do": "plan", "text": "Add IBAN and a finance step"}, **F)
        doc_key = self.wf.documents.get().key
        r = self.client.post(url, {"do": "apply"})
        self.assertRedirects(r, f"/workflows/{self.wf.pk}/builder/?ai=1", fetch_redirect_response=False)
        self.wf.refresh_from_db()
        steps = self.wf.ordered_steps()
        self.assertEqual([s.key for s in steps][:2], original_keys)  # unchanged steps keep their keys
        self.assertEqual((steps[1].name, steps[-1].name), ("Senior review", "Finance"))
        self.assertEqual(steps[0].recipient_type, "anyone")
        self.assertEqual(self.wf.documents.get().key, doc_key)
        self.assertIn("IBAN", [f.get("label") for f in self.wf.documents.get().fields])
        self.assertTrue(self.wf.has_unpublished_changes)
        self.assertEqual(self.wf.version, 1)  # not published
        self.assertEqual(Packet.objects.filter(workflow=self.wf).count(), 0)
        # undo restores the previous definition
        self.assertContains(self.client.get(url, **F), "Last AI change")
        self.client.post(url, {"do": "undo"})
        self.wf.refresh_from_db()
        self.assertEqual([s.key for s in self.wf.ordered_steps()], original_keys)
        self.assertEqual(self.wf.ordered_steps()[1].name, "Review")
        self.assertNotIn("IBAN", [f.get("label") for f in self.wf.documents.get().fields])
        self.assertNotIn("ai_undo", self.wf.options)

    def test_edit_operations_mode(self):
        from flows import ai_actions, ai_workflow
        before = ai_workflow.current_blueprint(self.wf)
        # rename + add after the old name + a field filled by the step added later in the same batch
        edits = [{"op": "update_step", "step": "Review", "set": {"name": "Senior review", "message_subject": "Please check"}},
                 {"op": "add_field", "field": {"label": "IBAN", "type": "text", "filled_by": ["IT setup"]}},
                 {"op": "add_step", "after": "Review", "step": {"name": "IT setup", "recipient": {"type": "email", "email": "it@x.test"}}},
                 {"op": "update_field", "field": "Fund", "set": {"required": True}},
                 {"op": "remove_step", "step": "Nope"}]
        out, changes, errors = ai_workflow.apply_edits(before, edits)
        names = [s["name"] for s in out["steps"]]
        self.assertEqual(names, ["Requester", "Senior review", "IT setup"])
        self.assertEqual(out["steps"][1]["subject"], "Please check")
        iban = next(f for f in out["documents"][0]["fields"] if f["label"] == "IBAN")
        self.assertEqual(iban["filled_by"], ["it_setup"])
        self.assertEqual(errors, ["no step “Nope”"])
        self.assertNotIn("_names", out)
        # one update pasted onto every step → only the step the request names keeps it
        same = {"subject": "Verify"}
        kept = ai_workflow._drop_copies(before, [{"op": "update_step", "step": "Requester", "set": same},
                                                 {"op": "update_step", "step": "Review", "set": same}], "Change the Review email")
        self.assertEqual([e["step"] for e in kept], ["Review"])
        # propose(): edits reply → diff; the prompt names steps (outline) and asks for edits
        with fake(json.dumps({"edits": edits[:1]})) as chat:
            r = ai_workflow.propose(self.wf, "Rename Review", self.owner)
        self.assertIn("Step “Review”", chat.call_args[0][2][0]["content"])
        self.assertEqual([(d["op"], d["name"]) for d in r["diff"]], [("changed", "Senior review")])
        # a reply that changes nothing is retried once with feedback, then reported
        replies = iter(['{"edits": []}', json.dumps({"edits": edits[:1]})])
        with fake(lambda *a, **k: next(replies)) as chat:
            r = ai_workflow.propose(self.wf, "Rename Review", self.owner)
        self.assertEqual(chat.call_count, 2)
        self.assertIn("did not work", chat.call_args[0][2][-1]["content"])
        with fake('{"edits": []}'):
            with self.assertRaises(ai_actions.AIError):
                ai_workflow.propose(self.wf, "Rename Review", self.owner)
        from flows.models import AIUsage
        row = AIUsage.objects.filter(workspace=self.ws, kind="workflow_edit").order_by("-id").first()
        self.assertFalse(row.ok)
        self.assertIn("changed nothing", row.error)
        self.assertIn("edits", row.detail)
        panel = self.client.get(f"/workspace/ai/usage/{row.pk}/")
        self.assertContains(panel, "Builder assistant")
        self.assertContains(panel, "Why it failed")
        self.assertContains(panel, "changed nothing")
        self.assertContains(panel, "edits")
        broken = AIUsage.objects.create(
            workspace=self.ws, user=self.owner, kind="workflow_edit", engine="ollama:test", ok=False,
            error="the reply was not valid JSON",
            detail=('{"edits":[{"op":"add_bot","bot":{"type":"email_custom","name":"Notify","when":{"logic":"or","rules":'
                    '[{"field":"Assessor decision","op":"eq","value":"Reject"},{"field":"Assessor decision","op":"eq",'
                    '"value":"Needs_Information"}]}}]}'))
        pretty = self.client.get(f"/workspace/ai/usage/{broken.pk}/")
        self.assertContains(pretty, "&quot;op&quot;: &quot;add_bot&quot;")
        self.assertContains(pretty, "Notify")

    def test_a_dropped_brace_still_parses(self):
        from flows import ai_builder
        missing = ('{"edits":[{"op":"add_bot","bot":{"type":"email_custom","name":"Notify","when":{"logic":"or","rules":'
                   '[{"field":"Assessor decision","op":"eq","value":"Reject"},{"field":"Assessor decision","op":"eq",'
                   '"value":"Needs_Information"}]}}]}')
        extra = ('{"edits":[{"op":"add_bot","bot":{"type":"email_custom","name":"Notify","when":{"logic":"or","rules":'
                 '[{"field":"Assessor decision","op":"eq","value":"Reject"},{"field":"Assessor decision","op":"eq",'
                 '"value":"Needs_Information"}]}}}]}}')
        for raw in (missing, extra):
            bot = ai_builder.extract_json(raw)["edits"][0]["bot"]
            self.assertEqual([r["value"] for r in bot["when"]["rules"]], ["Reject", "Needs_Information"])
        self.assertEqual(ai_builder.extract_json('{"edits":[]}')["edits"], [])

    def test_lane_only_edit_counts_as_a_change(self):
        from flows import ai_workflow
        before = {"name": "Claim", "documents": [{"key": "d", "name": "Form", "fields": []}],
                  "steps": [
                      {"key": "dec", "name": "Senior assessor decision", "type": "branch", "lanes": [
                          {"id": "no", "label": "Rejected", "when": {"rules": [{"field": "Decision", "op": "eq", "value": "Reject"}]},
                           "target": "end"}]},
                      {"key": "fin", "name": "Finance", "type": "step"}]}
        after = json.loads(json.dumps(before))
        after["steps"][0]["lanes"].append(
            {"id": "yes", "label": "Approved", "when": {"rules": [{"field": "Decision", "op": "eq", "value": "Approve"}]},
             "target": "fin"})
        added = [d for d in ai_workflow.diff(before, after) if d["what"] == "Lane" and d["op"] == "added"]
        self.assertEqual(len(added), 1)
        self.assertIn("Approved", added[0]["name"])
        self.assertEqual(added[0]["key"], "dec")
        after["steps"][0]["lanes"][0]["target"] = "fin"
        changed = [d for d in ai_workflow.diff(before, after) if d["op"] == "changed" and d["what"] == "Lane"]
        self.assertIn("target", changed[0]["props"])

    def test_assistant_needs_edit_rights_and_ai(self):
        self.client.force_login(User.objects.get(username="fay"))
        self.assertIn(self.client.get(f"/workflows/{self.wf.pk}/ai/").status_code, (403, 404))
        self.client.force_login(self.owner)
        self.ws.settings["ai"]["disabled"] = True
        self.ws.save()
        self.assertEqual(self.client.post(f"/workflows/{self.wf.pk}/ai/", {"do": "suggest"}).status_code, 403)
        self.assertNotContains(self.client.get(f"/workflows/{self.wf.pk}/builder/"), 'id="ai-panel"')
