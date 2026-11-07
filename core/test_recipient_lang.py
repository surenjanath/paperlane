"""Recipient pages in Spanish: page switch (remembered), workspace default, browser match; the team app stays English."""
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from accounts.models import Workspace
from core.test_deep import INTEG_BLUEPRINT

User = get_user_model()


class RecipientLanguageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        from flows import engine
        from flows.blueprint import build_workflow
        self.owner = User.objects.get(username="owner")
        self.ws = Workspace.objects.get(slug="demo")
        wf, _ = build_workflow(self.ws, self.owner, INTEG_BLUEPRINT, publish=True)
        p = engine.start_packet(wf, user=self.owner, source="api", first_assignee={"email": "x@client.test", "name": "X"})
        self.url = f"/f/{p.runs.get().token}/"

    def test_switch_and_remember(self):
        r = self.client.get(self.url)
        self.assertContains(r, "Save as draft")
        r = self.client.get(self.url + "?lang=es")
        self.assertContains(r, "Guardar borrador")
        self.assertContains(r, "Finalizar")
        self.assertEqual(r["Content-Language"], "es")
        self.assertContains(self.client.get(self.url), "Guardar borrador")  # remembered
        self.assertContains(self.client.get(self.url + "?lang=en"), "Save as draft")

    def test_workspace_default_and_browser(self):
        self.ws.settings = {**(self.ws.settings or {}), "recipient_language": "es"}
        self.ws.save()
        self.assertContains(self.client.get(self.url), "Guardar borrador")
        self.ws.settings["recipient_language"] = "auto"
        self.ws.save()
        self.assertContains(self.client.get(self.url, HTTP_ACCEPT_LANGUAGE="es-TT,es;q=0.9"), "Guardar borrador")
        self.assertContains(self.client.get(self.url, HTTP_ACCEPT_LANGUAGE="fr-FR"), "Save as draft")

    def test_team_app_stays_english_and_setting_saves(self):
        self.client.force_login(self.owner)
        self.client.get(self.url + "?lang=es")
        self.assertContains(self.client.get("/documents/"), "Documents")
        self.assertNotContains(self.client.get("/documents/"), "Guardar")
        self.client.post("/workspace/settings/", {"recipient_language": "es"})
        self.ws.refresh_from_db()
        self.assertEqual(self.ws.settings["recipient_language"], "es")
