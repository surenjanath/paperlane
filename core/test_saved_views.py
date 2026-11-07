"""Saved views on Documents: save the current filters, live counts, share with the team, delete rules."""
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from flows.models import SavedView

User = get_user_model()


class SavedViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def test_save_share_count_delete(self):
        owner, cal = User.objects.get(username="owner"), User.objects.get(username="cal")
        self.client.force_login(owner)
        page = self.client.get("/documents/?status=in_progress&page=2&junk=1")
        self.assertContains(page, "Save this view")
        self.client.post("/documents/views/", {"name": "Open work", "query": "status=in_progress&page=2&junk=1", "shared": "on"})
        self.client.post("/documents/views/", {"name": "Mine only", "query": "quick=mine"})
        v = SavedView.objects.get(name="Open work")
        self.assertEqual((v.query, v.shared), ("status=in_progress", True))  # only real filters are kept
        page = self.client.get("/documents/?status=in_progress")
        row = next(x for x in page.context["saved_views"] if x["v"].name == "Open work")
        self.assertTrue(row["on"])
        self.assertEqual(row["count"], page.context["page"].paginator.count)
        self.client.force_login(cal)  # a teammate sees the shared view, not the private one
        names = [x["v"].name for x in self.client.get("/documents/").context["saved_views"]]
        self.assertEqual(names, ["Open work"])
        self.assertEqual(self.client.post(f"/documents/views/{v.pk}/delete/").status_code, 403)
        self.client.force_login(owner)
        self.client.post(f"/documents/views/{v.pk}/delete/")
        self.assertFalse(SavedView.objects.filter(pk=v.pk).exists())
        self.assertEqual(self.client.post("/documents/views/", {"name": "", "query": ""}).status_code, 302)
