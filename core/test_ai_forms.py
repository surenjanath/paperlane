"""AI document builder: normalization, edits, review apply, file reading and the editor endpoint (model mocked)."""
import io
import json
import zipfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase

from accounts.models import Workspace
from core.test_deep import INTEG_BLUEPRINT
from flows import ai_forms

User = get_user_model()
ROLES = [{"key": "req", "name": "Claimant"}, {"key": "rev", "name": "Assessor"}]


class NormalizeTests(TestCase):
    def test_types_roles_conditions_formulas(self):
        raw = [{"key": "pay", "type": "select", "label": "Payment method", "options": ["Cheque", "Direct deposit"], "required": True},
               {"key": "acct", "type": "number", "label": "Bank account number", "show_if": {"field": "pay", "op": "eq", "value": "Direct deposit"}, "width": "half"},
               {"key": "a", "type": "currency", "label": "Amount", "role": "Assessor"},
               {"key": "fee", "type": "calc", "label": "Fee", "formula": "{amount} * 0.1"},
               {"type": "text"},
               {"type": "consent", "label": "Declaration"}]
        fields, notes = ai_forms.normalize(raw, ROLES, "req")
        by = {f["label"]: f for f in fields}
        self.assertEqual(by["Payment method"]["type"], "dropdown")
        self.assertEqual(by["Bank account number"]["type"], "text")  # identifiers keep leading zeros
        self.assertEqual(by["Bank account number"]["condition"]["rules"][0]["field"], by["Payment method"]["id"])
        self.assertEqual(by["Bank account number"]["width"], "half")
        self.assertEqual((by["Amount"]["role"], by["Payment method"]["role"]), ("rev", "req"))
        self.assertEqual(by["Fee"]["formula"], "{Amount} * 0.1")
        self.assertTrue(by["Declaration"]["required"])
        self.assertEqual(len(fields), 5)  # the untitled field was skipped
        self.assertTrue(any("untitled" in n for n in notes))
        self.assertEqual(len({f["name"] for f in fields if f["name"]}), 5)  # unique names for every input

    def test_apply_edits(self):
        fields, _ = ai_forms.normalize([{"type": "text", "label": "Name"}, {"type": "phone", "label": "Phone"},
                                        {"type": "radio", "label": "Pay by", "options": ["Cheque", "ACH"]}], ROLES, "req")
        res = ai_forms.apply_edits(fields, [
            {"op": "add", "after": "Phone", "fields": [{"type": "text", "label": "Emergency contact"}]},
            {"op": "update", "field": "phone", "set": {"required": True, "width": "half"}},
            {"op": "add", "after": "END", "fields": [{"type": "text", "label": "Routing number"}]},
            {"op": "update", "field": "Routing number", "set": {"show_if": {"field": "Pay by", "op": "eq", "value": "ACH"}}},
            {"op": "move", "field": "Pay by", "after": None}, {"op": "remove", "field": "Nope"}], ROLES)
        labels = [f["label"] for f in res["fields"]]
        self.assertEqual(labels, ["Pay by", "Name", "Phone", "Emergency contact", "Routing number"])
        self.assertTrue(res["fields"][2]["required"])
        self.assertEqual(res["fields"][4]["condition"]["rules"][0]["field"], res["fields"][0]["id"])
        self.assertIn("No field “Nope”.", res["notes"])

    def test_read_uploads(self):
        from reportlab.pdfgen import canvas
        buf = io.BytesIO()
        c = canvas.Canvas(buf)
        c.drawString(72, 720, "Claimant name: ________  Policy number: ______")
        c.save()
        self.assertIn("Policy number", ai_forms.text_from_upload(SimpleUploadedFile("f.pdf", buf.getvalue())))
        d = io.BytesIO()
        with zipfile.ZipFile(d, "w") as z:
            z.writestr("word/document.xml", "<w:document><w:p><w:t>Date of loss</w:t></w:p><w:p><w:t>Amount</w:t></w:p></w:document>")
        self.assertEqual(ai_forms.text_from_upload(SimpleUploadedFile("f.docx", d.getvalue())).split(), ["Date", "of", "loss", "Amount"])
        self.assertIsNone(ai_forms.text_from_upload(SimpleUploadedFile("f.png", b"\x89PNG....")))


class EditorAITests(TestCase):
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
        self.doc = self.wf.documents.get()
        self.url = f"/workflows/{self.wf.pk}/documents/{self.doc.pk}/editor/ai/"
        self.client.force_login(self.owner)
        self.roles = json.dumps([{"key": s.key, "name": s.name} for s in self.wf.ordered_steps()])

    def post(self, reply, **data):
        with mock.patch("flows.ai_providers.chat", return_value=json.dumps(reply)) as chat:
            r = self.client.post(self.url, {"fields": json.dumps(self.doc.fields), "roles": self.roles, "role": self.wf.ordered_steps()[0].key, **data})
        return r, chat

    def test_editor_has_panel(self):
        self.assertContains(self.client.get(f"/workflows/{self.wf.pk}/documents/{self.doc.pk}/editor/"), 'data-ai-url="' + self.url)

    def test_build_from_text_and_file(self):
        r, chat = self.post({"title": "claim_form", "fields": [{"type": "heading", "label": "You"}, {"type": "email", "label": "Email", "required": True}]},
                            action="build", text="A claim form")
        j = r.json()
        self.assertEqual((j["title"], [f["type"] for f in j["fields"]]), ("Claim form", ["heading", "email"]))
        self.assertIn("Requester", chat.call_args[0][2][0]["content"])  # roles are given to the model
        r, chat = self.post({"fields": [{"type": "text", "label": "Loss"}]}, action="build", text="",
                            file=SimpleUploadedFile("old.txt", b"Date of loss:\nDescription:"))
        self.assertIn("Date of loss", chat.call_args[0][2][0]["content"])
        self.assertEqual(self.post({}, action="build", text="")[0].status_code, 400)
        with mock.patch("flows.ai_forms.vision_engine", return_value=None):
            r, _ = self.post({}, action="build", file=SimpleUploadedFile("p.png", b"\x89PNG"))
        self.assertIn("vision model", r.json()["error"])

    def test_edit_review_apply_and_nothing_saved(self):
        before = list(self.doc.fields)
        r, _ = self.post({"edits": [{"op": "update", "field": "Holder", "set": {"required": True}}]}, action="edit", text="make holder required")
        self.assertTrue(next(f for f in r.json()["fields"] if f["label"] == "Holder")["required"])
        r, _ = self.post({"suggestions": [{"title": "Add email", "why": "x", "edits": [{"op": "add", "after": "END", "fields": [{"type": "email", "label": "Email"}]}]}]}, action="review")
        sug = r.json()["suggestions"][0]
        r, chat = self.post({}, action="apply", edits=json.dumps(sug["edits"]))
        chat.assert_not_called()
        self.assertEqual(r.json()["fields"][-1]["type"], "email")
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.fields, before)  # the endpoint never saves; the editor does

    def test_permissions_and_ai_off(self):
        self.client.force_login(User.objects.get(username="fay"))
        self.assertIn(self.post({}, action="review")[0].status_code, (403, 404))
        self.client.force_login(self.owner)
        self.ws.settings["ai"]["disabled"] = True
        self.ws.save()
        self.assertEqual(self.post({}, action="review")[0].status_code, 403)
