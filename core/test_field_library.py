"""Field library: starters listed, sections saved per workspace (roles stripped), delete, permissions."""
import json

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from flows.models import FieldBlock

User = get_user_model()


class FieldLibraryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def test_library(self):
        self.client.force_login(User.objects.get(username="owner"))
        j = self.client.get("/editor/library/").json()
        bank = next(b for b in j["blocks"] if b.get("key") == "bank")
        cond = bank["fields"][2]["condition"]["rules"][0]
        self.assertEqual(cond["field"], bank["fields"][1]["id"])  # starters reference their own fields
        self.assertTrue(bank["fields"][4]["sensitive"])
        r = self.client.post("/editor/library/", json.dumps({"name": "Vehicle", "fields": [
            {"id": "a", "type": "heading", "label": "Vehicle", "role": "x"}, {"id": "b", "type": "text", "label": "Plate", "role": "x"}]}),
            content_type="application/json")
        b = FieldBlock.objects.get(pk=r.json()["id"])
        self.assertNotIn("role", b.fields[1])
        self.assertEqual(self.client.get("/editor/library/").json()["blocks"][0]["name"], "Vehicle")
        self.client.post(f"/editor/library/?used={b.pk}")
        b.refresh_from_db()
        self.assertEqual(b.uses, 1)
        self.client.force_login(User.objects.get(username="ana"))  # auditor: read-only
        self.assertEqual(self.client.post(f"/editor/library/?delete={b.pk}").status_code, 403)
        self.client.force_login(User.objects.get(username="owner"))
        self.client.post(f"/editor/library/?delete={b.pk}")
        self.assertFalse(FieldBlock.objects.exists())
