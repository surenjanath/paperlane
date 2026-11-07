"""Field-level history: steps, bots and edits are recorded with who/what; sensitive values stay masked."""
import json

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from accounts.models import Workspace
from core.test_deep import INTEG_BLUEPRINT

User = get_user_model()


class HistoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def test_changes_recorded(self):
        from flows import engine, services
        from flows.blueprint import build_workflow
        from flows.history import changed_by
        from flows.mcp import Server
        from flows.models import Bot
        owner = User.objects.get(username="owner")
        ws = Workspace.objects.get(slug="demo")
        bp = json.loads(json.dumps(INTEG_BLUEPRINT))
        bp["documents"][0]["fields"].append({"id": "acct", "type": "text", "label": "Bank account", "sensitive": True, "filled_by": ["req"]})
        wf, _ = build_workflow(ws, owner, bp, publish=True)
        Bot.objects.create(workflow=wf, name="Set holder", bot_type="prefill_custom", trigger=Bot.STEP_COMPLETE,
                           step_key=wf.ordered_steps()[0].key, config={"mapping": "Holder = Riley"})
        services.publish(wf, owner)
        p = engine.start_packet(wf, user=owner, source="api", first_assignee={"email": "owner@paperlane.test", "name": "Olivia", "user": owner})
        Server(ws, owner).t_complete_step(p.pk, values={"Policy": "POL-1", "Bank account": "000123456789"})
        p.refresh_from_db()
        rows = {(c.label, c.source): c for c in p.field_changes.all()}
        self.assertEqual(rows[("Policy", "Step: Requester")].new, "POL-1")
        self.assertIn("Olivia", rows[("Policy", "Step: Requester")].actor_label + rows[("Policy", "Step: Requester")].actor.get_full_name())
        self.assertEqual(rows[("Holder", "Bot: Set holder")].new, "Riley")
        self.assertEqual(rows[("Bank account", "Step: Requester")].new, "•••• 6789")
        with changed_by(owner, "Olivia", "Revise"):
            p.data[next(iter(p.data))]["pol"] = "POL-2"
            p.save()
        last = p.field_changes.first()
        self.assertEqual((last.old, last.new, last.source), ("POL-1", "POL-2", "Revise"))
        self.client.force_login(owner)
        page = self.client.get(f"/documents/{p.pk}/?tab=history")
        self.assertContains(page, "POL-2")
        self.assertNotContains(page, "000123456789")
        self.assertEqual(len(self.client.get(f"/documents/{p.pk}/?tab=history&field=pol").context["changes"]), 2)
