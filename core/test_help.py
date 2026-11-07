"""Help center: pages, search, the AI chat (model mocked) and the article-only fallback."""
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from accounts.models import Workspace
from core import help_content as hc

User = get_user_model()


class HelpCenterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        self.owner = User.objects.get(username="owner")
        self.ws = Workspace.objects.get(slug="demo")
        self.client.force_login(self.owner)

    def test_pages_have_no_workspace_sidebar_and_every_article_renders(self):
        r = self.client.get("/help/")
        self.assertContains(r, "How can we help?")
        self.assertContains(r, 'id="hc-chat"')
        self.assertNotContains(r, 'class="sidebar')
        for a in hc.A:
            page = self.client.get(a["url"])
            self.assertContains(page, a["title"], msg_prefix=a["url"])
        self.assertRedirects(self.client.get("/pages/help/"), "/help/", fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/help/ai/"), hc.CAT_BY_SLUG["ai"]["articles"][0]["url"], fetch_redirect_response=False)
        self.assertEqual(self.client.get("/help/ai/nope/").status_code, 404)
        self.client.logout()
        self.assertContains(self.client.get("/help/workflows/approvals/"), "Approve / Reject")  # public too

    def test_search_ranks_titles_and_keywords(self):
        self.assertEqual(hc.search("add an approval with a reject branch")[0]["slug"], "approvals")
        self.assertEqual(hc.search("2fa")[0]["slug"], "security")
        self.assertEqual(hc.search("")[:1], [])
        r = self.client.get("/help/search.json", {"q": "webhook"}).json()
        self.assertEqual(r["results"][0]["url"], "/help/integrations/webhooks/")

    def test_ask_uses_articles_and_history(self):
        self.ws.settings = {**(self.ws.settings or {}), "ai": {"engine": "ollama:test"}}
        self.ws.save()
        with mock.patch("flows.ai_providers.chat", return_value="1. Open the builder") as chat:
            r = self.client.post("/help/ask/", {"question": "How do I reject?", "page": "Add an approval with Approve / Reject",
                                                "history": json.dumps([{"q": "hi", "a": "hello"}])})
        j = r.json()
        self.assertTrue(j["ai"])
        self.assertEqual(j["answer"], "1. Open the builder")
        self.assertEqual(j["sources"][0]["url"], "/help/workflows/approvals/")
        system, messages = chat.call_args[0][1], chat.call_args[0][2]
        self.assertIn("Add an approval with Approve / Reject", system)
        self.assertIn("Workspace Owner", system)
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user"])

    def test_ask_falls_back_to_articles_without_ai(self):
        self.ws.settings = {**(self.ws.settings or {}), "ai": {"engine": "ollama:test", "help_ai": False}}
        self.ws.save()
        with mock.patch("flows.ai_providers.chat") as chat:
            j = self.client.post("/help/ask/", {"question": "set up webhooks"}).json()
        chat.assert_not_called()
        self.assertFalse(j["ai"])
        self.assertEqual(j["sources"][0]["url"], "/help/integrations/webhooks/")
        self.assertContains(self.client.get("/help/"), "Finds help articles for you")
        self.client.logout()
        j = self.client.post("/help/ask/", {"question": "webhooks"}).json()  # visitors get articles, never the model
        self.assertFalse(j["ai"])
        self.assertEqual(self.client.post("/help/ask/", {"question": ""}).status_code, 400)


class HelpFeedbackTests(TestCase):
    def test_votes_once_per_session_and_admin_list(self):
        from core.models import HelpFeedback
        self.client.post("/help/feedback/", {"article": "workflows/approvals", "helpful": "0"})
        self.client.post("/help/feedback/", {"article": "workflows/approvals", "helpful": "0"})  # same session: ignored
        self.assertEqual(self.client.post("/help/feedback/", {"article": "nope/nope", "helpful": "1"}).status_code, 400)
        self.assertEqual(HelpFeedback.objects.count(), 1)
        admin = get_user_model().objects.create_superuser("root", "root@x.test", "pw-123456789")
        self.client.force_login(admin)
        self.assertContains(self.client.get("/platform/tickets/"), "Add an approval with Approve / Reject")
