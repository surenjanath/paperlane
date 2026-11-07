"""Build the "Insurance Claim" demo workflow with branch lanes: public-link intake → "Route by amount"
(lanes: Assessment / Senior assessment) → "Decision" (lanes: Approved → Finance; Needs more information → claimant,
then back to routing; Rejected → complete), plus completion bots. Publishes it and prints the links.

    python manage.py build_claims_workflow [--workspace demo] [--owner owner] [--replace]
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.urls import reverse

from accounts.models import Workspace
from flows import engine, services
from flows.models import Bot, Step, Workflow, WorkflowDocument

NAME = "Insurance Claim"


def fld(fid, type_, label, roles, required=False, **extra):
    return {"id": fid, "type": type_, "label": label, "name": label, "required": required, "role": roles, **extra}


def when(field_id, value, op="eq"):
    return {"logic": "and", "rules": [{"field": field_id, "op": op, "value": value}]}


class Command(BaseCommand):
    help = "Create (or recreate) the Insurance Claim demo workflow, publish it and print its start link."

    def add_arguments(self, parser):
        parser.add_argument("--workspace", default="demo")
        parser.add_argument("--owner", default="owner")
        parser.add_argument("--assessor", default="sam@paperlane.test")
        parser.add_argument("--senior", default="owner@paperlane.test")
        parser.add_argument("--finance", default="cal@paperlane.test")
        parser.add_argument("--replace", action="store_true", help="Trash an existing workflow with the same name first")

    @transaction.atomic
    def handle(self, *args, **o):
        ws = Workspace.objects.filter(slug=o["workspace"]).first()
        owner = get_user_model().objects.filter(username=o["owner"]).first()
        if ws is None or owner is None:
            raise CommandError("Workspace or owner not found (run seed_demo first).")
        existing = Workflow.objects.live().filter(workspace=ws, name=NAME)
        if existing.exists():
            if not o["replace"]:
                raise CommandError(f"“{NAME}” already exists — pass --replace to rebuild it.")
            from django.utils import timezone

            existing.update(trashed_at=timezone.now())
        wf = build(ws, owner, o["assessor"], o["senior"], o["finance"])
        problems = __import__("flows.views.builder", fromlist=["validate"]).validate(wf)
        if problems:
            raise CommandError("; ".join(problems))
        services.publish(wf, owner)
        wf.link_enabled = True
        wf.save(update_fields=["link_enabled"])
        self.stdout.write(self.style.SUCCESS(f"Published “{wf.name}” (id {wf.pk})"))
        self.stdout.write(f"Start link : {engine.absolute(reverse('flows:public_start', args=[wf.link_token]))}")
        self.stdout.write(f"Webhook    : {engine.absolute(reverse('flows:hook', args=[wf.hook_token]))}")
        self.stdout.write(f"Builder    : {engine.absolute(reverse('flows:builder', args=[wf.pk]))}")


def build(ws, owner, assessor, senior, finance):
    wf = Workflow.objects.create(workspace=ws, name=NAME, kind="workflow", created_by=owner,
                                 description="Public claim intake → assessment by amount → decision (approve / reject / "
                                             "more info loop) → payment.")
    mk = lambda order, **kw: Step.objects.create(workflow=wf, order=order, **kw)  # noqa: E731
    lane = lambda br, lid: {"lane": {"branch": br.key, "lane": lid}}  # noqa: E731
    claimant = mk(10, name="Claimant", recipient_type=Step.R_ANYONE)
    route = mk(20, name="Route by amount", kind=Step.BRANCH, branches=[
        {"id": "low", "label": "Up to 10,000", "logic": "and", "target": "lane", "then": "merge",
         "rules": [{"field": "Claimed amount", "op": "lte", "value": "10000"}]},
        {"id": "high", "label": "Over 10,000", "logic": "and", "target": "lane", "then": "merge",
         "rules": [{"field": "Claimed amount", "op": "gt", "value": "10000"}]}])
    assess = mk(30, name="Assessment", recipient_type=Step.R_EMAIL, recipient_email=assessor,
                message_subject="Claim to assess: {document_name}", settings=lane(route, "low"))
    senior_s = mk(40, name="Senior assessment", recipient_type=Step.R_EMAIL, recipient_email=senior,
                  message_subject="High-value claim to assess: {document_name}", settings=lane(route, "high"))
    decision = mk(50, name="Decision", kind=Step.BRANCH)
    more = mk(60, name="Claimant – more information", recipient_type=Step.R_EMAIL, recipient_email="{field:Email}",
              message_subject="We need more information about your claim",
              message_body="Hi {user_name},\nOur assessor needs more information:\n{field:Information requested}\n"
                           "Please update your claim using the button below.")
    pay = mk(70, name="Finance", recipient_type=Step.R_EMAIL, recipient_email=finance,
             message_subject="Approved claim to pay: {document_name}")
    C, A, S, M, F = claimant.key, assess.key, senior_s.key, more.key, pay.key
    claim_fields = [
        fld("c_h", "heading", "Insurance claim", "anyone", text="Tell us what happened. Fields marked * are required."),
        fld("c_name", "text", "Full name", [C], True, width="half"),
        fld("c_email", "email", "Email", [C], True, width="half"),
        fld("c_phone", "phone", "Phone", [C], width="half"),
        fld("c_policy", "text", "Policy number", [C], True, width="half", placeholder="POL-123456",
            pattern=r"POL-\d{6}", pattern_message="Use the format POL-123456.", help="Format POL-123456"),
        fld("c_type", "radio", "Claim type", [C], True, options=["Medical", "Property", "Vehicle"]),
        fld("c_date", "date", "Incident date", [C], True, width="half", date_format="dd/MM/yyyy"),
        fld("c_amount", "currency", "Claimed amount", [C], True, width="half", min=1),
        fld("c_hospital", "text", "Hospital or clinic", [C], True, condition=when("c_type", "Medical")),
        fld("c_vehicle", "text", "Vehicle registration", [C], True, condition=when("c_type", "Vehicle")),
        fld("c_police", "attachment", "Police report", [C], True, max_files=2, max_mb=10, condition=when("c_type", "Vehicle")),
        fld("c_desc", "textarea", "What happened?", [C, M], True),
        fld("c_docs", "attachment", "Supporting documents", [C, M], True, max_files=5, max_mb=20),
        fld("c_sig", "signature", "Claimant signature", [C], True),
    ]
    roles = [A, S]
    assessment_fields = [
        fld("a_h", "heading", "Assessment", "anyone"),
        fld("a_decision", "radio", "Decision", roles, True, options=["Approve", "Reject", "Need more info"]),
        fld("a_approved", "currency", "Approved amount", roles, True, width="half", min=0,
            condition=when("a_decision", "Approve")),
        fld("a_deduct", "number", "Deductible %", roles, False, width="half", default="10", min=0, max=100,
            condition=when("a_decision", "Approve")),
        fld("a_payout", "formula", "Payout", roles, formula="{Approved amount} * (1 - {Deductible %} / 100)",
            condition=when("a_decision", "Approve")),
        fld("a_info", "textarea", "Information requested", roles, True, condition=when("a_decision", "Need more info")),
        fld("a_reason", "textarea", "Rejection reason", roles, True, condition=when("a_decision", "Reject")),
        fld("a_sig", "signature", "Assessor signature", roles, True),
    ]
    payment_fields = [
        fld("p_h", "heading", "Payment", "anyone"),
        fld("p_method", "dropdown", "Payment method", [F], True, options=["Bank transfer", "Cheque"], width="half"),
        fld("p_account", "text", "Bank account", [F], True, width="half", condition=when("p_method", "Bank transfer")),
        fld("p_ref", "text", "Payment reference", [F], True, width="half", pattern=r"PAY-\d{4,}",
            pattern_message="Use the format PAY-0001."),
        fld("p_date", "date", "Paid on", [F], True, width="half"),
    ]
    d_claim = WorkflowDocument.objects.create(workflow=wf, name="Claim form", order=0, schema={"fields": claim_fields})
    d_assess = WorkflowDocument.objects.create(workflow=wf, name="Assessment", order=1, schema={"fields": assessment_fields})
    d_pay = WorkflowDocument.objects.create(workflow=wf, name="Payment", order=2, schema={"fields": payment_fields})
    dc, da, dp = d_claim.key, d_assess.key, d_pay.key

    def access(fill=(), view=(), hidden=()):
        return {**{k: "fill" for k in fill}, **{k: "view" for k in view}, **{k: "hidden" for k in hidden}}

    again = {"recurrence": True}
    claimant.document_access = access(fill=[dc], hidden=[da, dp])
    claimant.settings = {"tags": ["#NewClaim"]}
    claimant.thank_you = {"title": "Claim submitted", "body": "Thanks — an assessor will review your claim shortly."}
    reminder = {"enabled": True, "every_days": 1, "max": 3}
    for st in (assess, senior_s):
        st.document_access = access(fill=[da], view=[dc], hidden=[dp])
        st.settings = {**st.settings, **again, "reminder": reminder}
    decision.branches = [
        {"id": "approved", "label": "Approved", "logic": "and", "target": "lane", "then": "end",
         "rules": [{"field": "Decision", "op": "eq", "value": "Approve"}]},
        {"id": "info", "label": "Needs more information", "logic": "and", "target": "lane", "then": route.key,
         "rules": [{"field": "Decision", "op": "eq", "value": "Need more info"}]},
        {"id": "rejected", "label": "Rejected", "logic": "and", "target": "end", "then": "merge",
         "rules": [{"field": "Decision", "op": "eq", "value": "Reject"}]},
    ]
    more.document_access = access(fill=[dc], view=[da], hidden=[dp])
    more.settings = {**again, **lane(decision, "info")}
    pay.document_access = access(fill=[dp], view=[dc, da])
    pay.settings = {"forbid_decline": True, **lane(decision, "approved")}
    for s in (claimant, route, assess, senior_s, decision, more, pay):
        s.save()

    bots = [
        dict(bot_type="rename_packet", name="Name the claim", trigger=Bot.STEP_COMPLETE, step_key=C,
             config={"pattern": "Claim {field:Policy number} – {field:Full name}"}),
        dict(bot_type="add_tags", name="Flag high-value claims", trigger=Bot.STEP_COMPLETE, step_key=C,
             config={"tags": "#HighValue"}, condition={"logic": "and", "rules": [{"field": "Claimed amount", "op": "gt", "value": "10000"}]}),
        dict(bot_type="add_tags", name="Tag the decision", trigger=Bot.FLOW_COMPLETE, config={"tags": "#{field:Decision}"}),
        dict(bot_type="email_custom", name="Tell the claimant the outcome", trigger=Bot.FLOW_COMPLETE,
             config={"to": "{field:Email}", "subject": "Your claim {field:Policy number}: {field:Decision}",
                     "body": "Hi {field:Full name},\nThe decision on your claim is: {field:Decision}.\n{field:Rejection reason}"}),
        dict(bot_type="export_datasheet", name="Log to Claims sheet", trigger=Bot.FLOW_COMPLETE,
             config={"sheet": "Claims", "fields": "Policy number, Full name, Claim type, Claimed amount, Decision, Payout, Payment reference"}),
        dict(bot_type="lock_packet", name="Lock when finished", trigger=Bot.FLOW_COMPLETE, config={}),
    ]
    for i, b in enumerate(bots):
        Bot.objects.create(workflow=wf, order=i, enabled=True, **{**b, "config": {**b["config"], "skip_on_fail": True}})
    return wf
