import json

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Contact, Membership, Workspace
from flows import engine, services
from flows.bots import tick
from flows.models import Bot, DataSheet, Packet, Step, StepRun, Workflow

User = get_user_model()
PASSWORD = "paperlane-demo-2026"


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class SmokeTests(TestCase):
    """Crawl every page as the owner and exercise the main flows end to end."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)
        cls.owner = User.objects.get(username="owner")
        cls.ws = Workspace.objects.get(slug="demo")

    def setUp(self):
        self.client.force_login(self.owner)

    def get_ok(self, url, status=200):
        r = self.client.get(url)
        self.assertEqual(r.status_code, status, f"{url} → {r.status_code}")
        return r

    def test_submission_list_filters_and_opens_the_rest(self):
        from datetime import timedelta
        from flows.models import FormSubmission, IntakeForm
        form = IntakeForm.objects.create(workspace=self.ws, name="Intake filter", schema={"fields": [
            {"id": "n", "type": "text", "label": "Full name", "name": "Full name"},
            {"id": "e", "type": "email", "label": "Email", "name": "Email"},
            {"id": "note", "type": "textarea", "label": "Notes", "name": "Notes"},
        ]})
        ada = FormSubmission.objects.create(form=form, status="started", data={
            "values": {"n": "Ada Lovelace", "e": "ada@example.com", "note": "Roof leak detail"},
            "by_name": {"Full name": "Ada Lovelace", "Email": "ada@example.com", "Notes": "Roof leak detail"}})
        FormSubmission.objects.create(form=form, status="failed", error="workflow was not published", data={
            "values": {"n": "Grace Hopper", "e": "grace@example.com", "note": "Other note"}})
        old = FormSubmission.objects.create(form=form, status="saved", data={"values": {"n": "Old Entry", "e": "old@example.com"}})
        FormSubmission.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=40))
        page = self.client.get(reverse("flows:form_submissions", args=[form.pk]))
        self.assertContains(page, "Ada Lovelace")
        self.assertContains(page, "ada@example.com")
        self.assertNotContains(page, "Roof leak detail")
        self.assertContains(page, "All statuses")
        narrowed = self.client.get(reverse("flows:form_submissions", args=[form.pk]) + "?q=grace&status=failed")
        self.assertContains(narrowed, "Grace Hopper")
        self.assertNotContains(narrowed, "Ada Lovelace")
        recent = self.client.get(reverse("flows:form_submissions", args=[form.pk]) + "?from=" + timezone.localdate().isoformat())
        self.assertContains(recent, "Ada Lovelace")
        self.assertNotContains(recent, "Old Entry")
        csv = self.client.get(reverse("flows:form_submissions", args=[form.pk]) + "?format=csv&q=ada")
        self.assertContains(csv, "Roof leak detail")
        self.assertNotContains(csv, "Grace Hopper")
        panel = self.client.get(reverse("flows:form_submission", args=[form.pk, ada.pk]))
        self.assertContains(panel, "Roof leak detail")
        self.assertContains(panel, "ada@example.com")
        self.assertContains(panel, "Workflow started")
        other = IntakeForm.objects.create(workspace=self.ws, name="Other form", schema={"fields": []})
        self.assertEqual(self.client.get(reverse("flows:form_submission", args=[other.pk, ada.pk])).status_code, 404)

    def test_bulk_select_archives_workflows_and_forms(self):
        from flows.models import IntakeForm
        wfs = list(Workflow.objects.filter(workspace=self.ws, kind="workflow", trashed_at__isnull=True)[:2])
        page = self.client.get(reverse("flows:workflows"))
        self.assertContains(page, "Select all workflows")
        self.client.post(reverse("flows:workflows_bulk"), {"action": "hide", "ids": [w.pk for w in wfs]})
        for w in wfs:
            w.refresh_from_db()
            self.assertTrue(w.is_hidden)
        self.client.post(reverse("flows:workflows_bulk"), {"action": "unhide", "ids": [wfs[0].pk]})
        wfs[0].refresh_from_db()
        self.assertFalse(wfs[0].is_hidden)
        form = IntakeForm.objects.create(workspace=self.ws, name="Bulk me", schema={"fields": [{"id": "a", "type": "text", "label": "A"}]})
        forms_page = self.client.get(reverse("flows:forms"))
        self.assertContains(forms_page, "Select all forms")
        self.client.post(reverse("flows:forms_bulk"), {"action": "publish", "ids": [form.pk]})
        form.refresh_from_db()
        self.assertEqual(form.status, "published")
        self.client.post(reverse("flows:forms_bulk"), {"action": "archive", "ids": [form.pk]})
        form.refresh_from_db()
        self.assertIsNotNone(form.archived_at)

    def test_workflow_cards_name_their_triggers(self):
        from flows.models import IntakeForm
        wf = Workflow.objects.filter(workspace=self.ws, kind="workflow").first()
        wf.autostart = {"items": [
            {"id": "g", "type": "webhook", "enabled": True, "source": "Google Sheets update"},
            {"id": "s", "type": "schedule", "enabled": False, "repeat": "once"},
        ]}
        wf.link_enabled = True
        wf.save(update_fields=["autostart", "link_enabled"])
        IntakeForm.objects.create(workspace=self.ws, workflow=wf, name="Claim intake form")
        page = self.client.get(reverse("flows:workflows"))
        self.assertContains(page, "Form · Claim intake form")
        self.assertContains(page, "Google Sheets update")
        self.assertContains(page, "Scheduled start · paused")
        self.assertContains(page, "Shareable link")
        self.assertNotContains(self.client.get(reverse("flows:templates")), "Form · Claim intake form")

    def test_crawl_all_pages(self):
        wf = Workflow.objects.filter(workspace=self.ws, kind="workflow").first()
        tpl = Workflow.objects.filter(workspace=self.ws, kind="template").first()
        packet = Packet.objects.filter(workspace=self.ws).first()
        doc = wf.documents.first()
        step = wf.steps.first()
        urls = [
            reverse("core:dashboard"), reverse("core:analytics"), reverse("core:widget_new"), reverse("core:inbox"),
            reverse("core:inbox") + "?tab=waiting", reverse("core:inbox") + "?tab=completed",
            reverse("core:search") + "?q=a", reverse("core:search") + "?q=a&format=json",
            reverse("core:notifications"), reverse("core:notifications") + "?panel=1",
            reverse("core:archive"), reverse("core:trash"),
            reverse("flows:workflows"), reverse("flows:workflows") + "?sort=name&show=mine&hidden=1",
            reverse("flows:templates"), reverse("flows:documents"), reverse("flows:documents") + "?quick=attention",
            reverse("flows:documents") + "?quick=stale&sort=name&status=completed",
            reverse("flows:workflow_detail", args=[wf.pk]), reverse("flows:workflow_detail", args=[tpl.pk]),
            reverse("flows:run", args=[wf.pk]), reverse("flows:send_bulk", args=[wf.pk]),
            reverse("flows:sample_csv", args=[wf.pk]), reverse("flows:workflow_audit", args=[wf.pk]),
            reverse("flows:workflow_audit", args=[wf.pk]) + "?format=csv",
            reverse("flows:workflow_bots_log", args=[wf.pk]), reverse("flows:manage_access", args=[wf.pk]),
            reverse("flows:builder", args=[wf.pk]), reverse("flows:step_settings", args=[wf.pk, step.pk]),
            reverse("flows:preview_email", args=[wf.pk, step.pk]), reverse("flows:autostart", args=[wf.pk]),
            reverse("flows:doc_condition", args=[wf.pk, doc.pk]), reverse("flows:bots", args=[wf.pk]),
            reverse("flows:bot_catalog", args=[wf.pk]), reverse("flows:bot_catalog", args=[wf.pk]) + "?cat=All+bots",
            reverse("flows:bot_catalog", args=[wf.pk]) + "?q=salesforce",
            reverse("flows:editor", args=[wf.pk, doc.pk]), reverse("flows:preview", args=[wf.pk, doc.pk]),
            reverse("flows:packet_detail", args=[packet.pk]), reverse("flows:packet_download", args=[packet.pk]),
            reverse("flows:packet_download", args=[packet.pk]) + "?format=html",
            reverse("flows:packet_export", args=[packet.pk]), reverse("flows:packet_export", args=[packet.pk]) + "?kind=audit",
            reverse("flows:document_new"),
            reverse("accounts:profile"), reverse("accounts:settings"), reverse("accounts:notifications"),
            reverse("accounts:activity"), reverse("accounts:connections"), reverse("accounts:workspace_info"),
            reverse("accounts:workspace_settings"), reverse("accounts:theme"), reverse("accounts:thank_you"),
            reverse("accounts:email_templates"), reverse("accounts:email_templates") + "?key=reminder",
            reverse("accounts:domains"), reverse("accounts:subscription"), reverse("accounts:billing"),
            reverse("accounts:usage"), reverse("accounts:usage") + "?format=csv", reverse("accounts:email_log"),
            reverse("accounts:datasheets"), reverse("accounts:team"), reverse("accounts:team") + "?status=all&role=creator",
            reverse("accounts:groups"), reverse("accounts:contacts"), reverse("accounts:contact_new"),
            reverse("accounts:contacts_import"), reverse("accounts:contacts_export"), reverse("accounts:onboarding"),
            reverse("core:help"), reverse("core:help_article", args=["workflows", "approvals"]),
            reverse("core:page", args=["release-notes"]),
            reverse("core:page", args=["terms"]), reverse("core:page", args=["privacy"]),
        ]
        for tab in ["status", "data", "revisions", "comments", "files", "audit", "bots", "emails"]:
            urls.append(reverse("flows:packet_detail", args=[packet.pk]) + f"?tab={tab}")
        for w in self.client.get(reverse("core:analytics")).context["widgets"]:
            urls.append(reverse("core:widget_data", args=[w.pk]))
            urls.append(reverse("core:widget_edit", args=[w.pk]))
        for key in ["rename_packet", "webhook", "prefill_from_workflow", "export_salesforce", "save_google_drive", "export_datasheet"]:
            urls.append(reverse("flows:bot_new", args=[wf.pk]) + f"?type={key}")
        for url in urls:
            self.get_ok(url)
        rev = packet.revisions.first()
        if rev:
            self.get_ok(reverse("flows:packet_revision", args=[packet.pk, rev.number]))

    def test_full_workflow_lifecycle(self):
        c = self.client
        # create workflow → builder
        r = c.post(reverse("flows:create", args=["workflow"]), {"name": "Claims"})
        wf = Workflow.objects.get(name="Claims")
        self.assertRedirects(r, reverse("flows:builder", args=[wf.pk]))
        doc = wf.documents.first()
        step1 = wf.steps.first()
        # editor save: fields for two roles
        step2 = Step.objects.create(workflow=wf, name="Approver", order=20, recipient_email="sam@paperlane.test",
                                    settings={"reminder": {"enabled": True, "every_days": 0.5, "max": 2}})
        fields = [
            {"id": "h", "type": "heading", "label": "Claim"},
            {"id": "amt", "type": "currency", "label": "Amount", "name": "Amount", "required": True, "role": step1.key},
            {"id": "qty", "type": "number", "label": "Qty", "name": "Qty", "role": step1.key},
            {"id": "tot", "type": "formula", "label": "Total", "name": "Total", "formula": "{Amount} * {Qty}", "role": step1.key},
            {"id": "why", "type": "textarea", "label": "Why", "name": "Why", "role": step1.key,
             "condition": {"logic": "and", "rules": [{"field": "amt", "op": "gt", "value": "100"}]}, "required": True},
            {"id": "addr", "type": "address", "label": "Address", "name": "Address", "role": step1.key},
            {"id": "tbl", "type": "table", "label": "Items", "name": "Items", "columns": ["A", "B"], "role": step1.key},
            {"id": "sig", "type": "signature", "label": "Signature", "name": "Signature", "required": True, "role": step1.key},
            {"id": "pb", "type": "page_break", "label": "Approval"},
            {"id": "dec", "type": "radio", "label": "Decision", "name": "Decision", "options": ["Approved", "Rejected"],
             "required": True, "role": step2.key},
        ]
        r = c.post(reverse("flows:editor_save", args=[wf.pk, doc.pk]), json.dumps({"schema": {"fields": fields}, "design": {"width": "wide"}}),
                   content_type="application/json")
        self.assertEqual(r.json()["ok"], True)
        # step settings
        r = c.post(reverse("flows:step_settings", args=[wf.pk, step1.pk]), {
            "name": "Claimant", "recipient_type": "current_user", "message_subject": "Hi", "message_body": "Body",
            f"access_{doc.key}": "fill", "rule_logic": "and", "reminder_on": "on", "reminder_days": "0.001", "reminder_max": "2",
            "tags_on": "on", "tags": "#Claim"})
        self.assertEqual(r.status_code, 302)
        # branch + bots
        c.post(reverse("flows:step_add", args=[wf.pk]), {"kind": "branch", "after": step1.order})
        Bot.objects.create(workflow=wf, bot_type="rename_packet", name="Rename", trigger="flow_complete", config={"pattern": "Claim {field:Amount}"})
        Bot.objects.create(workflow=wf, bot_type="export_datasheet", name="Export", trigger="flow_complete", config={"sheet": "Claims"})
        Bot.objects.create(workflow=wf, bot_type="add_tags", name="Tag big", trigger="step_complete", config={"tags": "#Big"},
                           condition={"logic": "and", "rules": [{"field": "Amount", "op": "gt", "value": "100"}]})
        r = c.post(reverse("flows:publish", args=[wf.pk]))
        wf.refresh_from_db()
        self.assertTrue(wf.is_published)
        # run: first step = current user → redirect to fill
        r = c.post(reverse("flows:run", args=[wf.pk]), {"name": "Claim A"})
        packet = Packet.objects.get(name="Claim A")
        run = packet.runs.get()
        self.assertRedirects(r, reverse("flows:fill", args=[run.token]), fetch_redirect_response=False)
        self.get_ok(reverse("flows:fill", args=[run.token]))
        k = doc.key
        base = {f"f_{k}_amt": "250", f"f_{k}_qty": "2", f"f_{k}_addr__street": "1 Main", f"f_{k}_addr__city": "POS",
                f"f_{k}_tbl__r0__c0": "x", f"f_{k}_tbl__r0__c1": "y", f"f_{k}_tbl__r1__c0": "", f"f_{k}_tbl__r1__c1": "",
                f"f_{k}_sig": "data:image/png;base64,AAAA", "action": "submit"}
        # missing conditional required "why" → 400
        r = c.post(reverse("flows:fill", args=[run.token]), base)
        self.assertEqual(r.status_code, 400)
        # save draft
        c.post(reverse("flows:fill", args=[run.token]), dict(base, action="draft"))
        run.refresh_from_db()
        self.assertTrue(run.draft)
        r = c.post(reverse("flows:fill", args=[run.token]), dict(base, **{f"f_{k}_why": "Accident"}))
        self.assertEqual(r.status_code, 302)
        packet.refresh_from_db()
        self.assertEqual(packet.data[k]["tot"], "500")
        self.assertEqual(packet.data[k]["tbl"], [{"A": "x", "B": "y"}])
        self.assertIn("#Big", [t.name for t in packet.tags.all()])
        self.assertIn("#Claim", [t.name for t in packet.tags.all()])
        run2 = packet.runs.get(step_key=step2.key)
        self.assertEqual(run2.assignee_email, "sam@paperlane.test")
        self.assertTrue(any("sam@paperlane.test" in m.to for m in mail.outbox))
        # reminder via tick
        from django.utils import timezone
        StepRun.objects.filter(pk=run2.pk).update(created_at=timezone.now() - timezone.timedelta(days=1))
        self.assertGreaterEqual(tick()["reminders"], 1)
        # approver completes as anonymous
        c.logout()
        r = c.post(reverse("flows:fill", args=[run2.token]), {f"f_{k}_dec": "Approved", "action": "submit"})
        self.assertEqual(r.status_code, 302)
        packet.refresh_from_db()
        self.assertEqual(packet.status, "completed")
        self.assertEqual(packet.name, "Claim 250")
        self.assertTrue(DataSheet.objects.get(workspace=self.ws, name="Claims").rows.exists())
        self.get_ok(reverse("flows:fill_done", args=[run2.token]))
        self.get_ok(reverse("flows:fill", args=[run2.token]))  # closed page
        c.force_login(self.owner)
        r = self.get_ok(reverse("flows:packet_download", args=[packet.pk]))
        self.assertEqual(r["Content-Type"], "application/pdf")
        # revise back to step 2
        c.post(reverse("flows:packet_action", args=[packet.pk, "revise"]), {"step_key": step2.key})
        packet.refresh_from_db()
        self.assertEqual(packet.status, "in_progress")
        self.assertEqual(packet.current_step_key, step2.key)

    def test_branching_decline_link_hook_bulk(self):
        c = self.client
        wf = services.create_workflow(self.ws, self.owner, name="Branchy")
        s1 = wf.steps.first()
        s1.recipient_type = "anyone"
        s1.save()
        s2 = Step.objects.create(workflow=wf, name="Small", order=30, recipient_email="small@example.com")
        s3 = Step.objects.create(workflow=wf, name="Big", order=40, recipient_email="big@example.com")
        Step.objects.create(workflow=wf, kind="branch", name="Route", order=20, branches=[
            {"label": "big", "logic": "and", "rules": [{"field": "Amount", "op": "gte", "value": "1000"}], "target": s3.key}])
        doc = wf.documents.first()
        doc.schema = {"fields": [{"id": "a", "type": "number", "label": "Amount", "name": "Amount", "role": s1.key}]}
        doc.save()
        services.publish(wf, self.owner)
        wf.link_enabled = True
        wf.save()
        # public link start
        c.logout()
        self.get_ok(reverse("flows:public_start", args=[wf.link_token]))
        r = c.post(reverse("flows:public_start", args=[wf.link_token]), {"name": "Pat", "email": "pat@example.com"})
        packet = Packet.objects.filter(workflow=wf).latest("pk")
        run = packet.runs.get()
        r = c.post(reverse("flows:fill", args=[run.token]), {f"f_{doc.key}_a": "5000", "action": "submit"})
        packet.refresh_from_db()
        cur = packet.runs.filter(status="pending").get()
        self.assertEqual(cur.step_key, s3.key)  # branch jumped over "Small"
        # decline
        c.post(reverse("flows:fill", args=[cur.token]), {"action": "decline", "reason": "No"})
        packet.refresh_from_db()
        self.assertEqual(packet.status, "declined")
        # webhook autostart
        r = c.post(reverse("flows:hook", args=[wf.hook_token]), json.dumps({"Amount": 10, "recipient": "x@example.com"}),
                   content_type="application/json")
        self.assertTrue(r.json()["ok"])
        p2 = Packet.objects.get(pk=r.json()["document_id"])
        self.assertEqual(p2.flat_values()["Amount"], 10)
        # bulk send
        c.force_login(self.owner)
        r = c.post(reverse("flows:send_bulk", args=[wf.pk]), {"csv_text": "email,Amount\na@example.com,1\nb@example.com,2000\n"})
        self.assertEqual(Packet.objects.filter(workflow=wf, source="bulk").count(), 2)

    def test_library_actions_and_team(self):
        c = self.client
        wf = Workflow.objects.filter(workspace=self.ws, kind="workflow").first()
        for action in ["duplicate", "hide", "unhide", "archive", "restore", "trash", "restore"]:
            r = c.post(reverse("flows:workflow_action", args=[wf.pk, action]))
            self.assertEqual(r.status_code, 302, action)
        c.post(reverse("flows:workflow_action", args=[wf.pk, "tags"]), {"tags": "A, B"})
        self.assertEqual(wf.tags.count(), 2)
        c.post(reverse("accounts:team_invite"), {"emails": "new@example.com", "role": "filler"})
        self.assertTrue(any("new@example.com" in m.to for m in mail.outbox))
        m = Membership.objects.get(user__username="fay")
        c.post(reverse("accounts:team_member", args=[m.pk, "role"]), {"role": "creator"})
        m.refresh_from_db()
        self.assertEqual(m.role, "creator")
        c.post(reverse("accounts:contacts_import"), {"text": "name,email\nZed,zed@example.com\n"})
        self.assertTrue(Contact.objects.filter(email="zed@example.com").exists())
        c.post(reverse("accounts:groups"), {"name": "Ops", "members": [m.pk]})
        c.post(reverse("accounts:workspace_settings"), {"esign_consent": "on"})
        self.ws.refresh_from_db()
        self.assertTrue(self.ws.setting("esign_consent"))
        c.post(reverse("accounts:email_templates"), {"key": "invite", "subject": "S {user_name}", "body": "B"})
        r = self.get_ok(reverse("accounts:email_templates") + "?key=invite")
        self.assertContains(r, "Customized")

    def test_permissions(self):
        filler = User.objects.get(username="fay")
        self.client.force_login(filler)
        wf = Workflow.objects.filter(workspace=self.ws, kind="workflow", access="private").first()
        self.assertEqual(self.client.get(reverse("flows:workflow_detail", args=[wf.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("accounts:workspace_info")).status_code, 403)
        self.assertEqual(self.client.post(reverse("flows:create", args=["workflow"])).status_code, 403)
        auditor = User.objects.get(username="ana")
        self.client.force_login(auditor)
        self.get_ok(reverse("flows:workflow_detail", args=[wf.pk]))
        self.assertEqual(self.client.get(reverse("flows:builder", args=[wf.pk])).status_code, 403)

    def test_edit_lock(self):
        wf = Workflow.objects.filter(workspace=self.ws, kind="workflow").first()
        self.get_ok(reverse("flows:builder", args=[wf.pk]))
        sam = User.objects.get(username="sam")
        self.client.force_login(sam)
        r = self.get_ok(reverse("flows:builder", args=[wf.pk]))
        self.assertContains(r, "The process is in use")
        self.client.post(reverse("flows:request_access", args=[wf.pk]))
        self.assertTrue(self.owner.notifications.filter(kind="access_request").exists())
        r = self.get_ok(reverse("flows:builder", args=[wf.pk]) + "?takeover=1")
        self.assertNotContains(r, "The process is in use")

    def test_signup_onboarding(self):
        self.client.logout()
        r = self.client.post(reverse("accounts:signup"), {"first_name": "New", "email": "newbie@example.com", "password": "Very-secure-pass-77"})
        self.assertRedirects(r, reverse("accounts:onboarding"))
        r = self.client.get(reverse("core:dashboard"))
        self.assertRedirects(r, reverse("accounts:onboarding"))
        r = self.client.post(reverse("accounts:onboarding"), {"name": "Newco", "industry": "Finance", "size": "1-5"})
        self.assertRedirects(r, reverse("core:dashboard"))
        self.get_ok(reverse("core:analytics"))

    def test_copied_form_link_uses_the_open_address(self):
        from flows.models import IntakeForm
        form = IntakeForm.objects.create(workspace=self.ws, name="Link me", status="published", schema={"fields": [
            {"id": "n", "type": "text", "label": "Name"}]})
        page = self.client.get(reverse("flows:form_settings", args=[form.pk]))
        self.assertContains(page, f"http://testserver{form.public_url()}")
        self.assertNotContains(page, "127.0.0.1:8000")
        listed = self.client.get(reverse("flows:forms"))
        self.assertContains(listed, f"http://testserver{form.public_url()}")

    def test_complex_presets_include_a_form_and_branches(self):
        from flows.views.builder import validate
        created = [
            ("claim_settlement", "Claim intake and settlement", "Submit a claim"),
            ("vendor_onboarding", "Vendor onboarding and contract", "Vendor application"),
            ("capex_approval", "Capital expenditure approval", "Capital expenditure request"),
        ]
        for key, name, form_name in created:
            r = self.client.post(reverse("flows:create_preset", args=[key]))
            wf = Workflow.objects.filter(workspace=self.ws, name=name).latest("pk")
            self.assertRedirects(r, reverse("flows:builder", args=[wf.pk]))
            self.assertEqual(validate(wf), [])
            self.assertGreaterEqual(wf.steps.filter(kind="branch").count(), 2)
            self.assertGreaterEqual(wf.documents.count(), 3)
            form = wf.intake_forms.get()
            self.assertEqual(form.name, form_name)
            self.assertEqual(form.status, "published")
            self.assertTrue((form.options or {}).get("complete_first_step"))
            self.assertGreaterEqual(len(form.input_fields()), 8)
        home = self.client.get(reverse("core:dashboard"))
        for _, name, _form in created:
            self.assertContains(home, name)

    def test_preset_and_test_run(self):
        r = self.client.post(reverse("flows:create_preset", args=["expense_approval"]))
        wf = Workflow.objects.filter(workspace=self.ws, name="Expense approval").latest("pk")
        self.assertRedirects(r, reverse("flows:builder", args=[wf.pk]))
        r = self.client.post(reverse("flows:test_run", args=[wf.pk]))
        packet = Packet.objects.filter(workflow=wf, is_test=True).latest("pk")
        self.assertRedirects(r, reverse("flows:fill", args=[packet.runs.first().token]), fetch_redirect_response=False)

    def test_document_wizard(self):
        r = self.client.post(reverse("flows:document_new"), {"name": "NDA", "emails": ["x@example.com"], "names": ["Signer"]})
        wf = Workflow.objects.get(name="NDA", kind="document")
        self.assertRedirects(r, reverse("flows:editor", args=[wf.pk, wf.documents.first().pk]))
        self.get_ok(reverse("flows:document_send", args=[wf.pk]))
        self.client.post(reverse("flows:document_send", args=[wf.pk]))
        self.assertTrue(Packet.objects.filter(workflow=wf).exists())
        self.assertNotIn(wf, self.client.get(reverse("flows:workflows")).context["groups"][0][1])



class WorkflowExportImportTests(TestCase):
    """A Paperlane export round-trips through import, and the import screen accepts the file."""

    @classmethod
    def setUpTestData(cls):
        import io
        call_command("seed_demo", verbosity=0)
        call_command("build_claims_workflow", verbosity=0, stdout=io.StringIO())
        cls.owner = User.objects.get(username="owner")
        cls.ws = Workspace.objects.get(slug="demo")
        cls.wf = Workflow.objects.live().get(workspace=cls.ws, name="Insurance Claim")

    def test_export_roundtrip_and_ui(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from flows.importers import export_workflow, import_any

        data = json.loads(json.dumps(export_workflow(self.wf)))
        clone, _ = import_any(data, self.ws, self.owner)
        self.assertEqual(clone.steps.count(), self.wf.steps.count())
        self.assertEqual(clone.bots.count(), self.wf.bots.count())
        self.assertEqual(clone.routing_mode, self.wf.routing_mode)
        self.client.force_login(self.owner)
        r = self.client.get(reverse("flows:workflow_export", args=[self.wf.pk]))
        self.assertEqual(r["Content-Type"], "application/json")
        before = Workflow.objects.count()
        r = self.client.post(reverse("flows:workflow_import"), {"file": SimpleUploadedFile("claim.json", r.content)})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Workflow.objects.count(), before + 1)
        bot = self.wf.bots.first()
        self.assertEqual(self.client.get(reverse("flows:bot_edit", args=[self.wf.pk, bot.pk])).status_code, 200)

    def test_unknown_file_is_rejected(self):
        from flows.importers import import_any

        with self.assertRaises(ValueError):
            import_any({"flow": {}, "addons": []}, self.ws, self.owner)
