"""Workflow version comparison."""
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from accounts.models import Workspace
from core.test_deep import INTEG_BLUEPRINT
from flows import services, versions

User = get_user_model()


class VersionCompareTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def test_diff_versions_and_draft(self):
        from flows.blueprint import build_workflow
        owner = User.objects.get(username="owner")
        ws = Workspace.objects.get(slug="demo")
        wf, _ = build_workflow(ws, owner, INTEG_BLUEPRINT, publish=True)
        v1 = wf.versions.get().snapshot
        step = wf.ordered_steps()[1]
        step.name, step.recipient_email = "Senior review", "boss@paperlane.test"
        step.save()
        doc = wf.documents.get()
        fields = doc.schema["fields"]
        fields[1]["required"] = True
        fields.append({"id": "iban", "type": "text", "label": "IBAN"})
        doc.save()
        rows = versions.diff(v1, wf.build_snapshot())
        got = {(r["op"], r["what"], r["name"]): r.get("props") for r in rows}
        self.assertIn(("added", "Field", "IBAN (Claim)"), got)
        self.assertEqual(got[("changed", "Step", "Senior review")], ["name", "recipient"])
        self.assertEqual(got[("changed", "Field", "Holder (Claim)")], ["required"])
        self.assertEqual(versions.diff(v1, v1), [])
        services.publish(wf, owner)
        self.client.force_login(owner)
        r = self.client.get(f"/workflows/{wf.pk}/compare/?a=1&b=2")
        self.assertContains(r, "Senior review")
        self.assertContains(r, "1 added")
