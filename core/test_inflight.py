"""In-flight safety: a case stays on the workflow version it started on, and one group task can't be approved twice.

The race tests reproduce what two browsers do: each loads the step while it's open (a stale copy of the run), then both
submit. Before the fix both submits completed the step, the packet advanced twice — skipping the next approver
entirely — and the audit trail showed two approvals."""
import io
import zipfile

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import TestCase

from accounts.models import Group, Membership, Workspace
from core.docx_fixture import build, p as para
from flows import engine, services
from flows.blueprint import build_workflow
from flows.mcp import Server
from flows.models import AuditEvent, Step, StepRun, WorkflowDocument

User = get_user_model()
BP = {"name": "Spend approval", "documents": [{"key": "d", "name": "Request", "fields": [
    {"id": "amt", "type": "currency", "label": "Amount", "required": True, "filled_by": ["req"]},
    {"id": "dec", "type": "radio", "label": "Decision", "options": ["Approve", "Reject"], "required": True, "filled_by": ["mgr"]}]}],
    "steps": [{"key": "req", "name": "Requester", "recipient": {"type": "anyone"}},
              {"key": "mgr", "name": "Managers", "recipient": {"type": "group", "group": "Approvers"}},
              {"key": "fin", "name": "Finance", "recipient": {"type": "email", "email": "ana@paperlane.test"}}]}


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        self.ws = Workspace.objects.get(slug="demo")
        self.owner = User.objects.get(username="owner")
        self.sam, self.fay = User.objects.get(username="sam"), User.objects.get(username="fay")
        self.wf, _ = build_workflow(self.ws, self.owner, BP, publish=True)
        Group.objects.get(workspace=self.ws, name="Approvers").members.add(
            *Membership.objects.filter(workspace=self.ws, user__in=[self.sam, self.fay]))

    def start(self):
        p = engine.start_packet(self.wf, user=self.owner, source="api",
                                first_assignee={"email": self.owner.email, "name": "O", "user": self.owner})
        Server(self.ws, self.owner).t_complete_step(p.pk, values={"Amount": "2500"})
        p.refresh_from_db()
        return p

    def open_run(self, p):
        return p.runs.get(status__in=[StepRun.PENDING, StepRun.OPENED])

    @staticmethod
    def field(p, label):
        """(doc key, field id) of a field in the packet's frozen definition — the builder assigns its own ids."""
        return next((d["key"], f["id"]) for d in p.documents for f in d["fields"] if f.get("label") == label)

    def decide(self, p, choice):
        dk, fid = self.field(p, "Decision")
        return {dk: {fid: choice}}

    def decision(self, p):
        dk, fid = self.field(p, "Decision")
        return p.data[dk][fid]


class DoubleApprovalTests(Base):
    def assert_single_approval(self, p, winner):
        p.refresh_from_db()
        self.assertEqual(p.status, "in_progress")
        mgr = p.runs.get(step_name="Managers")
        self.assertEqual((mgr.status, mgr.assignee_user, mgr.claimed_by), ("completed", winner, winner))
        self.assertEqual(AuditEvent.objects.filter(packet=p, action="step.completed", detail__contains="Managers").count(), 1)
        self.assertEqual(p.runs.filter(step_name="Finance").count(), 1)  # advanced exactly once
        self.assertTrue(p.runs.filter(step_name="Finance", status="pending").exists())  # Finance still has to approve
        self.assertEqual(p.revisions.filter(step_name="Managers").count(), 1)

    def test_engine_refuses_a_stale_second_completion(self):
        p = self.start()
        a, b = self.open_run(p), self.open_run(p)  # two browsers, both saw it open
        engine.complete_run(a, self.decide(p, "Approve"), user=self.fay)
        with self.assertRaisesMessage(engine.StepClosed, "Fay"):
            engine.complete_run(b, self.decide(p, "Reject"), user=self.sam)
        self.assert_single_approval(p, self.fay)
        self.assertEqual(self.decision(p), "Approve")  # the loser's answer didn't overwrite

    def test_stale_decline_after_approval_is_refused(self):
        p = self.start()
        a, b = self.open_run(p), self.open_run(p)
        engine.complete_run(a, self.decide(p, "Approve"), user=self.fay)
        with self.assertRaises(engine.StepClosed):
            engine.decline_run(b, "no", user=self.sam)
        self.assert_single_approval(p, self.fay)

    def test_quick_approvals_from_two_browsers(self):
        from django.test import Client

        p = self.start()
        token = self.open_run(p).token
        fay, sam = Client(), Client()
        fay.force_login(self.fay)
        sam.force_login(self.sam)
        stale = StepRun.objects.get(token=token)  # what Sam's request read before Fay's committed
        self.assertEqual(fay.post(f"/approvals/{token}/", {"decision": "Approve"}).status_code, 302)
        with self.assertRaises(engine.StepClosed):
            engine.complete_run(stale, self.decide(p, "Approve"), user=self.sam)
        r = sam.post(f"/approvals/{token}/", {"decision": "Approve"}, follow=True)
        self.assertContains(r, "already completed")
        self.assert_single_approval(p, self.fay)

    def test_fill_page_loser_sees_who_won(self):
        from django.test import Client

        p = self.start()
        run = self.open_run(p)
        stale = self.open_run(p)
        engine.complete_run(run, self.decide(p, "Approve"), user=self.fay)
        sam = Client()
        sam.force_login(self.sam)
        from django.test import RequestFactory

        from flows.views import fill as fill_view

        dk, fid = self.field(p, "Decision")
        req = RequestFactory().post(f"/f/{stale.token}/", {f"f_{dk}_{fid}": "Reject"})
        req.user, req.session = self.sam, sam.session
        from django.contrib.messages.storage.fallback import FallbackStorage

        req._messages = FallbackStorage(req)
        resp = fill_view._handle_post(req, stale, self.sam)
        self.assertEqual(resp.status_code, 409)
        self.assertIn(b"Fay Filler", resp.content)  # told who has it, not shown a second approval
        self.assert_single_approval(p, self.fay)

    def test_claim_is_first_come(self):
        p = self.start()
        a, b = self.open_run(p), self.open_run(p)
        self.assertTrue(a.claim(self.fay))
        self.assertFalse(b.claim(self.sam))  # the stale copy can't steal it
        self.assertEqual(b.claimed_by, self.fay)
        self.assertTrue(b.claim(self.sam, force=True))  # a manager takeover still works
        with self.assertRaisesMessage(engine.StepClosed, "Sam"):
            engine.complete_run(a, self.decide(p, "Approve"), user=self.fay)


class InFlightVersionTests(Base):
    def edit_and_republish(self):
        doc = self.wf.documents.get(name="Request")
        schema = doc.schema
        dec = next(f for f in schema["fields"] if f.get("label") == "Decision")
        dec["options"] = ["Approve", "Reject", "Escalate"]
        schema["fields"].append({"id": "cc", "type": "text", "label": "Cost centre", "required": True, "role": dec["role"]})
        doc.schema = schema
        doc.save()
        mgr = self.wf.steps.get(name="Managers")
        mgr.name, mgr.recipient_type, mgr.recipient_email, mgr.recipient_group = "Directors", Step.R_EMAIL, self.owner.email, None
        mgr.save()
        Step.objects.create(workflow=self.wf, order=999, name="Legal", recipient_type=Step.R_EMAIL, recipient_email="ana@paperlane.test")
        self.wf.touch()
        services.publish(self.wf, self.owner)
        self.wf.refresh_from_db()

    def test_old_case_finishes_on_its_own_version(self):
        old = self.start()
        self.edit_and_republish()
        new = self.start()
        self.assertEqual((old.version, new.version), (1, 2))
        # the waiting approver still gets the v1 form: no Escalate option, no new required field
        self.client.force_login(self.fay)
        run = self.open_run(old)
        page = self.client.get(f"/f/{run.token}/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "Escalate")
        self.assertNotContains(page, "Cost centre")
        dk, fid = self.field(old, "Decision")
        self.assertEqual(next(f for f in new.documents[0]["fields"] if f["id"] == fid)["options"], ["Approve", "Reject", "Escalate"])
        self.assertTrue(any(f.get("label") == "Cost centre" for f in new.documents[0]["fields"]))  # v2 really changed
        r = self.client.post(f"/f/{run.token}/", {f"f_{dk}_{fid}": "Approve", "consent": "on"})
        self.assertEqual(r.status_code, 302)
        old.refresh_from_db()
        # and routes on v1: group step done by a group member, then v1's Finance — not v2's Directors / Legal
        self.assertEqual([r.step_name for r in old.runs.order_by("id")], ["Requester", "Managers", "Finance"])
        engine.complete_run(self.open_run(old), {}, user=User.objects.get(email="ana@paperlane.test"))
        old.refresh_from_db()
        self.assertEqual(old.status, "completed")
        self.assertFalse(old.runs.filter(step_name__in=["Directors", "Legal"]).exists())
        # the new case uses v2
        self.assertEqual(self.open_run(new).step_name, "Directors")
        self.assertEqual([s["name"] for s in new.steps], ["Requester", "Directors", "Finance", "Legal"])

    def test_unchanged_republish_does_not_bump_version(self):
        services.publish(self.wf, self.owner)
        self.wf.refresh_from_db()
        self.assertEqual(self.wf.version, 1)

    def test_word_template_comes_from_the_packets_version(self):
        from flows.bots import h_docx_template

        tpl = WorkflowDocument.objects.create(workflow=self.wf, name="Letter.docx", kind=WorkflowDocument.FILE, schema={"fields": []})
        tpl.file.save("Letter.docx", ContentFile(build([para("Version ONE for {Amount}")])))
        self.wf.touch()
        services.publish(self.wf, self.owner)
        old = self.start()
        tpl.file.save("Letter.docx", ContentFile(build([para("Version TWO for {Amount}")])))  # replaced in the editor
        self.wf.touch()
        services.publish(self.wf, self.owner)
        new = self.start()
        for packet, want in ((old, "Version ONE"), (new, "Version TWO")):
            h_docx_template(packet, {"template": "Letter"}, {})
            f = packet.files.get(doc_key="generated")
            with f.file.open("rb") as fh:
                xml = zipfile.ZipFile(io.BytesIO(fh.read())).read("word/document.xml").decode()
            self.assertIn(want, xml)
