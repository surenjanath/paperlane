"""Ask your documents (keyword + meaning, citations, visibility), the watchdog and the weekly operations note."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from accounts.models import Membership, Workspace
from core.test_deep import INTEG_BLUEPRINT

User = get_user_model()


class AIRound6Tests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        from flows.blueprint import build_workflow

        self.owner = User.objects.get(username="owner")
        self.ws = Workspace.objects.get(slug="demo")
        self.ws.settings = {**(self.ws.settings or {}), "ai": {"engine": "ollama:test", "embed_engine": "off"}}
        self.ws.save()
        self.m = Membership.objects.get(workspace=self.ws, user=self.owner)
        self.wf, _ = build_workflow(self.ws, self.owner, INTEG_BLUEPRINT, publish=True)
        self.client.force_login(self.owner)

    def packet(self, values):
        from flows import engine

        p = engine.start_packet(self.wf, user=self.owner, source="api",
                                first_assignee={"email": "owner@paperlane.test", "name": "Owner", "user": self.owner})
        doc = p.documents[0]
        by_label = {(f.get("label") or f.get("name")): f["id"] for f in doc["fields"]}
        p.data = {doc["key"]: {by_label[k]: v for k, v in values.items() if k in by_label}}
        p.save()
        return p

    def text_field(self):
        doc = self.wf.documents.first()
        return next(f.get("label") or f.get("name") for f in doc.input_fields() if f.get("type") in ("text", "textarea") and not f.get("sensitive"))

    # ---- ask your documents ----
    def test_keyword_search_ranks_and_answers_with_citations(self):
        label = self.text_field()
        water = self.packet({label: "Burst pipe caused water damage in the kitchen"})
        self.packet({label: "Stolen bicycle from the garage"})
        from flows import semantic

        hits = semantic.search(self.m, "water damage claims")
        self.assertEqual(hits[0]["packet"].pk, water.pk)
        with mock.patch("flows.ai_providers.chat", return_value=f"One claim mentions water damage [#{water.pk}]."):
            r = self.client.get("/documents/ask/", {"q": "water damage claims"})
        self.assertContains(r, f"[#{water.pk}]")
        self.assertContains(r, "Cited")

    def test_meaning_search_uses_vectors_when_an_embedding_model_exists(self):
        label = self.text_field()
        a = self.packet({label: "Employee onboarding forms for a new hire"})
        b = self.packet({label: "Quarterly sales invoice"})
        self.ws.settings["ai"]["embed_engine"] = "ollama:emb"
        self.ws.save()
        from flows import semantic

        def fake_embed(engine, texts):
            return [[1.0, 0.0] if ("hire" in t or "staff" in t) else [0.0, 1.0] for t in texts]

        with mock.patch("flows.ai_providers.embedding_engines", return_value=[{"id": "ollama:emb", "label": "emb"}]), \
                mock.patch("flows.ai_providers.embed", side_effect=fake_embed):
            semantic.refresh(self.ws)
            hits = semantic.search(self.m, "paperwork for joining staff")  # shares no words with document a
        self.assertEqual(hits[0]["packet"].pk, a.pk)
        self.assertEqual(hits[0]["why"], "meaning")
        self.assertNotIn(b.pk, [h["packet"].pk for h in hits[:1]])

    def test_search_never_shows_documents_the_person_cannot_see(self):
        label = self.text_field()
        p = self.packet({label: "Confidential merger paperwork"})
        outsider = User.objects.create_user("filler-x", "filler-x@example.com", "x")
        m = Membership.objects.create(workspace=self.ws, user=outsider, role=Membership.FILLER)
        from flows import semantic

        self.assertFalse(m.can_view_all)
        self.assertNotIn(p.pk, [h["packet"].pk for h in semantic.search(m, "merger paperwork")])

    def test_sensitive_fields_are_not_indexed(self):
        doc = self.wf.documents.first()
        doc.schema = {**doc.schema, "fields": [*doc.fields, {"id": "ssn", "type": "text", "label": "SSN", "name": "SSN",
                                                             "role": "anyone", "sensitive": True}]}
        doc.save()
        from flows import services, semantic

        services.publish(self.wf, self.owner)
        p = self.packet({"SSN": "123-45-6789"})
        self.assertNotIn("123-45-6789", semantic.doc_text(p))

    # ---- watchdog ----
    def test_watchdog_flags_unusual_amounts_and_repeats_once(self):
        from flows import watchdog

        self.ws.settings["ai"]["watchdog"] = True
        self.ws.save()
        doc = self.wf.documents.first()
        doc.schema = {**doc.schema, "fields": [*doc.fields, {"id": "amt", "type": "currency", "label": "Amount", "name": "Amount", "role": "anyone"},
                                               {"id": "em", "type": "email", "label": "Email", "name": "Email", "role": "anyone"}]}
        doc.save()
        from flows import services

        services.publish(self.wf, self.owner)
        for i in range(10):
            self.packet({"Amount": str(1000 + i * 10), "Email": f"person{i}@example.com"})
        normal = self.packet({"Amount": "1040", "Email": "someone@example.com"})
        self.assertEqual(watchdog.inspect(normal), [])
        odd = self.packet({"Amount": "95000", "Email": "repeat@example.com"})
        self.packet({"Email": "repeat@example.com"})
        self.packet({"Email": "repeat@example.com"})
        found = watchdog.inspect(odd)
        kinds = {f["kind"] for f in found}
        self.assertEqual(kinds, {"amount", "repeat"})
        self.assertEqual(watchdog.inspect(odd), [])  # announced once
        page = self.client.get(f"/documents/{odd.pk}/")
        self.assertContains(page, "Watchdog")
        fid = found[0]["id"]
        self.client.post(f"/documents/{odd.pk}/action/watchdog_dismiss/", {"finding": fid})
        odd.refresh_from_db()
        self.assertTrue(next(w for w in odd.flags["watchdog"] if w["id"] == fid)["dismissed"])

    def test_watchdog_is_off_by_default(self):
        from flows import watchdog

        self.assertEqual(watchdog.inspect(self.packet({})), [])

    # ---- weekly operations note ----
    def test_ops_summary_is_written_once_a_day_and_lands_in_the_team_report(self):
        from django.core.cache import cache

        from flows import digests

        cache.clear()
        self.packet({})
        with mock.patch("flows.ai_providers.chat", return_value="A calm week with one new claim.") as chat:
            r = self.client.get("/team/workload/summary/").json()
            self.client.get("/team/workload/summary/")
        self.assertEqual(r["text"], "A calm week with one new claim.")
        self.assertEqual(chat.call_count, 1)
        self.assertTrue(any("Started" in f for f in r["facts"]))
        subject, body = digests.team(self.ws, __import__("django.utils.timezone", fromlist=["now"]).now() - __import__("datetime").timedelta(days=7))
        self.assertIn("A calm week", body)
