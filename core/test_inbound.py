"""Start workflows from email: provider webhooks and the stdin pipe, AI fill (never sensitive fields), attachments,
the sender's review link, and the guards (secret, members/domains, duplicates, unpublished)."""
import base64
import io
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings

from accounts.models import Workspace

User = get_user_model()
TOKEN = "abcdef123456"
BP = {"name": "Claims by email", "documents": [{"key": "d", "name": "Claim", "fields": [
    {"id": "nm", "type": "text", "label": "Full name", "filled_by": ["req"]},
    {"id": "amt", "type": "currency", "label": "Amount", "filled_by": ["req"]},
    {"id": "items", "type": "table", "label": "Items", "columns": ["Date", "Description", "Amount"], "filled_by": ["req"]},
    {"id": "acct", "type": "text", "label": "Account number", "sensitive": True, "filled_by": ["req"]},
    {"id": "dec", "type": "radio", "label": "Decision", "options": ["Approve", "Reject"], "filled_by": ["mgr"]}]}],
    "steps": [{"key": "req", "name": "Claimant", "recipient": {"type": "anyone"}},
              {"key": "mgr", "name": "Manager", "recipient": {"type": "email", "email": "sam@paperlane.test"}}]}
MIME = (b"From: Olivia Owner <owner@paperlane.test>\r\nTo: claims+" + TOKEN.encode() + b"@in.test\r\nSubject: Claim for Riley\r\n"
        b"Message-ID: <pipe-1@mail>\r\nContent-Type: text/plain\r\n\r\nPlease file a claim for Riley, amount 250.\r\n")


@override_settings(PAPERLANE_INBOUND_SECRET="s3cret", PAPERLANE_INBOUND_DOMAIN="in.test")
class InboundTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        from flows.blueprint import build_workflow
        self.owner = User.objects.get(username="owner")
        ws = Workspace.objects.get(slug="demo")
        ws.settings = {**(ws.settings or {}), "ai": {"engine": "ollama:test"}}
        ws.save()
        self.wf, _ = build_workflow(ws, self.owner, BP, publish=True)
        self.wf.autostart = {"items": [{"id": "e1", "type": "email", "token": TOKEN, "enabled": True, "senders": "members", "ai_fill": True}]}
        self.wf.save(update_fields=["autostart"])
        self.prompts = []

        def fake_chat(ws, engine, system, prompt, **kw):
            self.prompts.append(prompt if isinstance(prompt, str) else "\n".join(m.get("content", "") for m in prompt))
            return json.dumps({"Full name": "Riley Demo", "Amount": "250", "Account number": "999",
                               "Items": [{"description": "Hotel", "amount": 1640.0, "junk": 1}, ["Oct 9", "Taxi", 380], {}]})
        self.ai = mock.patch("flows.ai_actions.chat", side_effect=fake_chat)
        self.ai.start()
        self.addCleanup(self.ai.stop)

    def post(self, data, secret="s3cret", **kw):
        return self.client.post(f"/inbound/email/{secret}/", data, **kw)

    def test_mailgun_starts_and_fills(self):
        from flows.models import Packet
        r = self.post({"recipient": f"{TOKEN}@in.test", "from": "Olivia <owner@paperlane.test>", "subject": "Claim for Riley",
                       "body-plain": "Riley claims 250.", "Message-Id": "<m1@mail>",
                       "attachment-1": SimpleUploadedFile("receipt.pdf", b"%PDF-1.4 x", content_type="application/pdf")})
        self.assertTrue(r.json()["ok"], r.json())
        p = Packet.objects.get(pk=r.json()["document"])
        vals = p.flat_values()
        self.assertEqual((p.name, vals["Full name"], vals["Amount"]), ("Claim for Riley", "Riley Demo", "250"))
        self.assertEqual(vals["Items"], [{"Date": "", "Description": "Hotel", "Amount": "1640"},
                                         {"Date": "Oct 9", "Description": "Taxi", "Amount": "380"}])  # rows keyed by the real columns
        self.assertIn('"columns": ["Date", "Description", "Amount"]', self.prompts[0])
        self.assertNotIn("Account number", vals)  # the model's answer for a sensitive field is ignored…
        self.assertNotIn("Account number", self.prompts[0])  # …and it was never asked for
        self.assertEqual(p.files.get().original_name, "receipt.pdf")
        self.assertIn("Riley claims 250.", p.comments.get().text)
        run = p.runs.get(status__in=["pending", "opened"])
        self.assertEqual(run.assignee_email, "owner@paperlane.test")
        self.assertTrue(any(run.fill_url() in m.body or run.token in m.body for m in mail.outbox))
        self.assertEqual(p.field_changes.filter(label="Full name").first().source, "Email")
        self.assertEqual(self.post({"recipient": f"{TOKEN}@in.test", "from": "owner@paperlane.test", "Message-Id": "<m1@mail>"}).json()["ok"], False)
        self.wf.refresh_from_db()
        self.assertEqual(self.wf.autostart["items"][0]["count"], 1)

    def test_postmark_json_and_pipe(self):
        from flows.models import Packet
        body = {"ToFull": [{"Email": f"x+{TOKEN}@in.test"}], "FromFull": {"Email": "owner@paperlane.test", "Name": "Olivia"},
                "Subject": "Postmark claim", "TextBody": "Riley, 250", "MessageID": "pm-1",
                "Attachments": [{"Name": "a.txt", "Content": base64.b64encode(b"hello").decode(), "ContentType": "text/plain"}]}
        r = self.post(json.dumps(body), content_type="application/json")
        self.assertTrue(r.json()["ok"], r.json())
        self.assertEqual(Packet.objects.get(pk=r.json()["document"]).files.count(), 1)
        out = io.StringIO()
        with mock.patch("sys.stdin", io.TextIOWrapper(io.BytesIO(MIME))):
            call_command("inbound_email", stdout=out)
        self.assertIn("Started document", out.getvalue())
        self.assertTrue(Packet.objects.filter(name="Claim for Riley", source="email").exists())

    def test_guards(self):
        ok = {"recipient": f"{TOKEN}@in.test", "from": "owner@paperlane.test", "subject": "x", "body-plain": "y"}
        self.assertEqual(self.post(ok, secret="wrong").status_code, 404)
        with override_settings(PAPERLANE_INBOUND_SECRET=""):
            self.assertEqual(self.post(ok, secret="").status_code, 404)
        self.assertIn("may not start", self.post({**ok, "from": "stranger@evil.test"}).json()["reason"])
        self.assertIn("no workflow", self.post({**ok, "recipient": "nobody@in.test"}).json()["reason"])
        item = self.wf.autostart["items"][0]
        self.wf.autostart = {"items": [{**item, "senders": "domains", "domains": "partner.org"}]}
        self.wf.save(update_fields=["autostart"])
        r = self.post({**ok, "from": "ann@partner.org", "Message-Id": "<d1>"})
        self.assertTrue(r.json()["ok"], r.json())
        from flows.models import Packet
        self.assertIsNone(Packet.objects.get(pk=r.json()["document"]).created_by)
        self.wf.status = "draft"
        self.wf.save(update_fields=["status"])
        self.assertIn("off", self.post({**ok, "Message-Id": "<d2>"}).json()["reason"])

    def test_autostart_panel(self):
        self.client.force_login(self.owner)
        r = self.client.post(f"/workflows/{self.wf.pk}/builder/autostart/", {"type": "email", "senders": "domains", "domains": "", "ai_fill": "on"})
        self.assertEqual(r.status_code, 302)
        self.wf.refresh_from_db()
        self.assertEqual(len(self.wf.autostart["items"]), 1)  # refused: domains mode needs domains
        self.client.post(f"/workflows/{self.wf.pk}/builder/autostart/", {"type": "email", "senders": "members", "ai_fill": "on"})
        self.wf.refresh_from_db()
        new = self.wf.autostart["items"][-1]
        self.assertEqual((new["type"], len(new["token"]), new["ai_fill"]), ("email", 12, True))
        page = self.client.get(f"/workflows/{self.wf.pk}/builder/autostart/?item={new['id']}")
        self.assertContains(page, f"{new['token']}@in.test")
