"""Word templates, identity checks, one-tap decisions, cloud drives and the insights funnel."""
import io
import zipfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import TestCase

from accounts.models import Connection, Workspace
from core.docx_fixture import sample
from core.test_deep import INTEG_BLUEPRINT

User = get_user_model()

APPROVAL_BP = {
    "name": "Purchase approval",
    "documents": [{"key": "f", "name": "Request", "fields": [
        {"id": "item", "type": "text", "label": "Item", "required": True, "filled_by": ["req"]},
        {"id": "amount", "type": "currency", "label": "Amount", "filled_by": ["req"]},
        {"id": "decision", "type": "radio", "label": "Manager decision", "options": ["Approve", "Reject"], "required": True,
         "filled_by": ["mgr"]},
        {"id": "note", "type": "textarea", "label": "Manager comment", "filled_by": ["mgr"]}]}],
    "steps": [{"key": "req", "name": "Requester", "recipient": {"type": "anyone"}},
              {"key": "mgr", "name": "Manager", "recipient": {"type": "email", "email": "boss@example.com"}}],
}


class ProductRound7Tests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        from django.core.cache import cache

        from flows.blueprint import build_workflow

        cache.clear()  # identity-check locks and codes are keyed by run id, which repeats between tests

        self.owner = User.objects.get(username="owner")
        self.ws = Workspace.objects.get(slug="demo")
        self.wf, _ = build_workflow(self.ws, self.owner, INTEG_BLUEPRINT, publish=True)
        self.client.force_login(self.owner)

    def start(self, wf=None, email="client@example.com"):
        from flows import engine

        return engine.start_packet(wf or self.wf, user=self.owner, source="api", first_assignee={"email": email, "name": "Client"})

    # ---- 1. Word templates ----
    def test_docx_template_fills_conditions_and_tables(self):
        from flows import docgen

        out, missing = docgen.render(sample(), {"holder": "Riley & Co", "fund": "9000", "priority": "Low", "policy": "POL-1",
                                                "document_name": "CLM-7"})
        xml = zipfile.ZipFile(io.BytesIO(out)).read("word/document.xml").decode()
        self.assertIn("Dear Riley &amp; Co,", xml)          # split runs joined, value escaped
        self.assertNotIn("HIGH priority", xml)                 # false multi-paragraph block removed
        self.assertIn("Inline: big.", xml)
        self.assertIn("Senior review", xml)                    # true block inside a table kept
        self.assertNotIn("{if", xml)
        self.assertEqual(missing, ["No such field"])
        low, _ = docgen.render(sample(), {"fund": "250"})
        self.assertNotIn("Senior review", zipfile.ZipFile(io.BytesIO(low)).read("word/document.xml").decode())
        with self.assertRaises(docgen.TemplateError):
            docgen.render(b"not a zip", {})

    def test_docx_bot_saves_the_filled_file_to_the_document(self):
        from flows.bots import h_docx_template
        from flows.models import WorkflowDocument

        tpl = WorkflowDocument.objects.create(workflow=self.wf, name="Letter.docx", kind=WorkflowDocument.FILE, schema={"fields": []})
        tpl.file.save("Letter.docx", ContentFile(sample()))
        p = self.start()
        msg = h_docx_template(p, {"template": "Letter", "filename": "{document_name} letter"}, {})
        self.assertIn("Generated", msg)
        f = p.files.get(doc_key="generated")
        self.assertTrue(f.original_name.endswith(".docx"))
        with self.assertRaisesMessage(RuntimeError, "No uploaded Word template"):
            h_docx_template(p, {"template": "Missing"}, {})

    # ---- 2. identity checks ----
    def set_verify(self, **rule):
        from flows import services

        step = self.wf.ordered_steps()[0]
        step.settings = {**(step.settings or {}), "verify": rule}
        step.save()
        services.publish(self.wf, self.owner)

    def test_email_code_guards_the_documents(self):
        self.set_verify(method="email")
        p = self.start()
        run = p.runs.get(status__in=["pending", "opened"])
        self.client.logout()
        url = run.fill_url()
        r = self.client.get(url)
        self.assertContains(r, "Verify it")
        mail.outbox.clear()
        self.client.post(url, {"idv": "send"})
        code = next(m for m in mail.outbox if "code" in m.subject.lower()).body.split("code is ")[1][:6]
        r = self.client.post(url, {"idv": "check", "code": "000000" if code != "000000" else "111111"})
        self.assertContains(r, "That code")
        r = self.client.post(url, {"idv": "check", "code": code}, follow=True)
        self.assertNotContains(r, "Verify it")

    def test_access_code_from_a_field_and_lockout(self):
        self.set_verify(method="code", code="{field:Policy}")
        p = self.start()
        doc = p.documents[0]
        pol = next(f["id"] for f in doc["fields"] if (f.get("label") or f.get("name")) == "Policy")
        p.data = {doc["key"]: {pol: "POL-42"}}
        p.save()
        run = p.runs.get(status__in=["pending", "opened"])
        self.client.logout()
        url = run.fill_url()
        for _ in range(5):
            self.client.post(url, {"idv": "check", "code": "wrong"})
        r = self.client.post(url, {"idv": "check", "code": "pol-42"})
        self.assertContains(r, "Too many wrong attempts")  # locked even with the right answer

    # ---- 3. one-tap decisions ----
    def test_one_tap_decision_from_the_email(self):
        from flows import engine
        from flows.blueprint import build_workflow
        from flows.models import StepRun

        wf, _ = build_workflow(self.ws, self.owner, APPROVAL_BP, publish=True)
        p = engine.start_packet(wf, user=self.owner, source="api", first_assignee={"email": "req@example.com", "name": "Req"})
        mail.outbox.clear()
        run = p.runs.get(status__in=["pending", "opened"])
        doc = p.documents[0]
        ids = {(f.get("label")): f["id"] for f in doc["fields"]}
        engine.complete_run(run, {doc["key"]: {ids["Item"]: "Laptop", ids["Amount"]: "900"}})
        invite = next(m for m in mail.outbox if "boss@example.com" in m.to)
        self.assertIn("Decide straight from this email", invite.body)
        mgr = p.runs.get(step_name="Manager")
        link = f"/decide/{mgr.token}/0/"
        self.client.logout()
        r = self.client.get(link)
        self.assertContains(r, "Confirm: Approve")
        mgr.refresh_from_db()
        self.assertNotEqual(mgr.status, StepRun.COMPLETED)  # opening the link decides nothing
        r = self.client.post(link, {"comment": "OK"})
        self.assertContains(r, "recorded: Approve")
        mgr.refresh_from_db()
        p.refresh_from_db()
        self.assertEqual(mgr.status, StepRun.COMPLETED)
        flat = p.flat_values()
        self.assertEqual(flat["Manager decision"], "Approve")
        self.assertEqual(flat["Manager comment"], "OK")

    # ---- 4. cloud drives ----
    def test_save_to_drive_uploads_to_microsoft_with_folder_from_fields(self):
        import time

        from flows.bots import h_save_to_drive

        conn = Connection.objects.create(user=self.owner, workspace=self.ws, kind="microsoft", label="OneDrive",
                                         data={"access_token": "t", "refresh_token": "r", "expires_at": time.time() + 3600})
        p = self.start()
        calls = []

        def fake_request(method, url, token, body=None, headers=None):
            calls.append((method, url, token))
            return {"webUrl": "https://contoso.sharepoint.com/x.pdf"}

        with mock.patch("flows.clouddrive._request", side_effect=fake_request), \
                mock.patch("flows.exports.packet_pdf", return_value=b"%PDF-1.4"):
            msg = h_save_to_drive(p, {"connection": conn.pk, "folder": "Claims/{workflow_name}"}, {})
        self.assertIn("Saved 1 file", msg)
        method, url, token = calls[0]
        self.assertEqual((method, token), ("PUT", "t"))
        self.assertIn("/me/drive/root:/Claims/Integration%20test/", url)

    def test_expired_token_is_refreshed(self):
        from flows import clouddrive

        conn = Connection.objects.create(user=self.owner, workspace=self.ws, kind="google", label="Drive",
                                         data={"access_token": "old", "refresh_token": "r", "expires_at": 0})
        with mock.patch("flows.clouddrive._post_form", return_value={"access_token": "new", "expires_in": 3600}):
            self.assertEqual(clouddrive.access_token(conn), "new")
        conn.refresh_from_db()
        self.assertEqual(conn.data["refresh_token"], "r")

    def test_oauth_callback_rejects_a_forged_state(self):
        r = self.client.get("/account/connections/oauth/google/callback/", {"state": "forged", "code": "x"}, follow=True)
        self.assertContains(r, "expired")
        self.assertFalse(Connection.objects.filter(kind="google").exists())

    # ---- 5. insights ----
    def test_insights_funnel_weekly_and_csv(self):
        self.start()
        r = self.client.get(f"/workflows/{self.wf.pk}/insights/")
        self.assertContains(r, "Where documents stop")
        self.assertContains(r, "waiting")
        csv_resp = self.client.get(f"/workflows/{self.wf.pk}/insights/", {"format": "csv"})
        self.assertEqual(csv_resp["Content-Type"], "text/csv")
        self.assertIn(b"Reached %", csv_resp.content)
