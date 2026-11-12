"""Bank example: a complete personal-loan process connected to a (fictional) core-banking database.

    python manage.py seed_bank_demo [--workspace demo] [--replace]

What it builds (see examples/bank-loan/README.md for the full tour):
  * Connector "Core banking (sample)": the read-only SQLite file examples/bank-loan/bank.sqlite3 (40 customers,
    accounts, loans and credit-bureau records). Actions: customer (by customer number) and credit (credit check).
  * Workflow "Personal loan": application (start form with a customer lookup) → ID check refresh when the ID
    check has expired → loan officer review (the credit check runs automatically) → declined / more information /
    continue → compliance screening and credit committee for large or higher-risk loans → the client signs the loan
    offer after an email code → disbursement → key dates for the first repayment and maturity. Decline letters
    and the signed offer are emailed.
  * Start form "Apply for a personal loan" (public link) that fills the customer's details from the bank database.
  * Ten applications from real rows of the sample database, caught at every stage.
"""
import datetime as dt
import random

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test.utils import override_settings
from django.utils import timezone

from accounts.models import Group, Membership, Profile, Workspace
from flows import connectors, engine
from flows.blueprint import build_form, build_workflow
from flows.models import AuditEvent, Bot, Connector, IntakeForm, Packet, StepRun, Workflow

User = get_user_model()
WF_NAME = "Personal loan"
FORM_NAME = "Apply for a personal loan"
CONNECTOR_NAME = "Core banking (sample)"
DATABASE = "bank-loan/bank.sqlite3"

TEAM = [  # username, first, last, position, groups
    ("lina", "Lina", "Ortiz", "Loan officer", ["Loan officers"]),
    ("omar", "Omar", "Haynes", "Head of credit", ["Credit committee"]),
    ("ruth", "Ruth", "Mendes", "Compliance officer", ["Compliance"]),
    ("kai", "Kai", "Brennan", "Customer service", ["Customer service"]),
    ("priya", "Priya", "Patel", "Finance officer", ["Loan operations"]),
]
DEMO_MEMBERS = {"Loan officers": ["fay"], "Credit committee": ["sam"], "Compliance": ["cal"], "Customer service": ["fay"],
                "Loan operations": ["owner"]}  # so the one-click demo accounts can work every queue

# The public form only finds a customer when the customer number AND the date of birth match, so a guessed number
# reveals nothing. ({value} is the date of birth the visitor typed; both are bound as SQL parameters.)
CUSTOMER_SQL = """SELECT customer_id, full_name, email, phone, address, employer, job_title, monthly_income,
       existing_monthly_debt, kyc_status, kyc_expires, current_account, segment, customer_since
FROM customer_summary
WHERE upper(customer_id) = upper(trim({field:Customer number})) AND date_of_birth = {value}"""
SEED_SQL = "SELECT * FROM customer_summary WHERE customer_id = {value}"  # seeding only, never exposed
CREDIT_SQL = """SELECT score, band, defaults_5y, enquiries_6m, max_days_past_due, avg_balance_6m, last_checked
FROM credit_view WHERE customer_id = {field:Customer number}"""

PAYMENT = "round({amount} * ({rate}/1200) / (1 - (1 + {rate}/1200) ** -{term}), 2)"
OFFER_PAYMENT = "round({offer_amount} * ({offer_rate}/1200) / (1 - (1 + {offer_rate}/1200) ** -{term}), 2)"
DTI = ("round(({existing_debt} + {amount} * ({rate}/1200) / (1 - (1 + {rate}/1200) ** -{term})) "
       "/ max({monthly_income}, 1) * 100, 1)")


def F(fid, type_, label, by, required=True, **extra):
    return {"id": fid, "type": type_, "label": label, "name": label, "required": required, "filled_by": by, **extra}


def when(field, op, value):
    return {"logic": "and", "rules": [{"field": field, "op": op, "value": value}]}


def any_of(*rules):
    return {"logic": "or", "rules": [{"field": f, "op": o, "value": v} for f, o, v in rules]}


APPLICANT = ["applicant", "info"]
CUSTOMER_FIELDS = [  # filled from the bank database, read-only for the applicant
    F("customer_id", "text", "Customer number", APPLICANT, width="half", placeholder="CUS-10001"),
    F("dob", "date", "Date of birth", APPLICANT, width="half"),
    F("full_name", "text", "Full name", APPLICANT, readonly=True),
    F("email", "email", "Email", APPLICANT, width="half", readonly=True),
    F("phone", "phone", "Phone", APPLICANT, width="half"),
    F("address", "text", "Address", APPLICANT, readonly=True),
    F("employer", "text", "Employer", APPLICANT, width="half", readonly=True),
    F("job_title", "text", "Job title", APPLICANT, width="half", readonly=True),
    F("monthly_income", "currency", "Monthly income", APPLICANT, width="half", readonly=True),
    F("existing_debt", "currency", "Existing monthly debt", APPLICANT, required=False, width="half", readonly=True),
    F("kyc_status", "text", "ID check status", APPLICANT + ["kyc"], width="half", readonly=True),
    F("kyc_expires", "date", "ID check expires", ["kyc"], required=False, width="half"),
    F("current_account", "text", "Current account", APPLICANT, required=False, width="half", readonly=True),
    F("segment", "text", "Segment", APPLICANT, required=False, width="half", readonly=True),
]
LOAN_FIELDS = [
    F("product", "dropdown", "Loan type", APPLICANT, width="half",
      options=["Personal loan", "Car loan", "Home improvement", "Debt consolidation"]),
    F("amount", "currency", "Loan amount", APPLICANT, width="half"),
    F("term", "dropdown", "Term (months)", APPLICANT, width="half", options=["12", "24", "36", "48", "60", "72", "84"]),
    F("rate", "number", "Indicative rate (%)", APPLICANT, required=False, width="half", readonly=True, default="11.5"),
    F("monthly_payment", "formula", "Estimated monthly payment", APPLICANT, required=False, width="half", formula=PAYMENT),
    F("dti", "formula", "Debt to income (%)", APPLICANT, required=False, width="half", formula=DTI),
    F("purpose", "textarea", "What is the loan for?", APPLICANT),
    F("payslip", "attachment", "Latest payslip", APPLICANT, required=False, max_files=3, max_mb=10),
    F("id_doc", "attachment", "Photo ID", APPLICANT, required=False, max_files=2, max_mb=10),
    F("consent", "clickwrap", "I authorise a credit check and confirm the information I gave is true.", ["applicant"]),
    F("app_sig", "signature", "Applicant signature", ["applicant"]),
]

BLUEPRINT = {
    "name": WF_NAME,
    "description": "A personal loan from application to money in the account: customer lookup in core banking, "
                   "automatic credit check, officer review, compliance and credit committee for larger or riskier "
                   "loans, a signed offer letter and disbursement.",
    "documents": [
        {"key": "application", "name": "Loan application", "fields": [
            # the officer's request is part of the application, so the customer never sees the credit assessment
            F("info_request", "textarea", "Message from your loan officer", ["officer"], required=False,
              condition=when("Officer recommendation", "eq", "Need more information")),
            {"id": "h_customer", "type": "heading", "label": "Your details", "text": "Filled in from your account.", "filled_by": ["applicant"]},
            *CUSTOMER_FIELDS,
            {"id": "h_loan", "type": "heading", "label": "Your loan", "text": "Tell us what you need.", "filled_by": ["applicant"]},
            *LOAN_FIELDS]},
        {"key": "assessment", "name": "Credit assessment", "fields": [
            F("score", "number", "Credit score", ["officer"], width="half"),
            F("band", "text", "Credit band", ["officer"], width="half"),
            F("defaults", "number", "Defaults in 5 years", ["officer"], width="half"),
            F("dpd", "number", "Most days past due", ["officer"], width="half"),
            F("enquiries", "number", "Credit enquiries (6 months)", ["officer"], required=False, width="half"),
            F("avg_balance", "currency", "Average balance (6 months)", ["officer"], required=False, width="half"),
            F("id_ok", "checkbox", "ID and documents checked", ["officer"], options=["Yes"]),
            F("income_ok", "radio", "Income verified", ["officer"], options=["Yes", "No"], width="half"),
            F("recommendation", "radio", "Officer recommendation", ["officer"],
              options=["Approve", "Decline", "Need more information"]),
            F("offer_amount", "currency", "Offered amount", ["officer"], width="half",
              condition=when("Officer recommendation", "eq", "Approve")),
            F("offer_rate", "number", "Offered rate (%)", ["officer"], width="half",
              condition=when("Officer recommendation", "eq", "Approve")),
            F("officer_notes", "textarea", "Officer notes", ["officer"], required=False)]},
        {"key": "compliance", "name": "Compliance screening", "fields": [
            F("screening", "radio", "Sanctions and PEP screening", ["aml"], options=["Clear", "Escalate"]),
            F("aml_notes", "textarea", "Compliance notes", ["aml"], required=False)]},
        {"key": "committee", "name": "Credit committee", "fields": [
            F("committee_decision", "radio", "Committee decision", ["committee"],
              options=["Approved", "Approved with conditions", "Declined"]),
            F("conditions", "textarea", "Conditions", ["committee"], required=False,
              condition=when("Committee decision", "eq", "Approved with conditions")),
            F("committee_notes", "textarea", "Committee notes", ["committee"], required=False)]},
        {"key": "offer", "name": "Loan offer letter", "fields": [
            {"id": "h_offer", "type": "heading", "label": "Your loan offer", "text": "Your loan offer", "filled_by": ["offer"]},
            {"id": "offer_intro", "type": "description", "label": "Offer", "filled_by": ["offer"],
             "text": "We are pleased to offer you the loan below. Please read the terms, then accept and sign. "
                     "The money is paid into your current account once you have signed."},
            F("o_amount", "formula", "Loan amount", ["offer"], required=False, width="half", formula="{offer_amount}"),
            F("o_rate", "formula", "Interest rate (% a year)", ["offer"], required=False, width="half", formula="{offer_rate}"),
            F("o_term", "formula", "Term (months)", ["offer"], required=False, width="half", formula="{term}"),
            F("o_payment", "formula", "Monthly repayment", ["offer"], required=False, width="half", formula=OFFER_PAYMENT),
            F("o_total", "formula", "Total to repay", ["offer"], required=False, width="half",
              formula=f"round({OFFER_PAYMENT[6:-4]} * {{term}}, 2)"),
            {"id": "offer_terms", "type": "description", "label": "Terms", "filled_by": ["offer"],
             "text": "Repayments are taken from your current account on the same day each month. You may repay early "
                     "at any time without a fee. A late payment may be reported to the credit bureau."},
            F("accept", "clickwrap", "I accept this loan offer and its terms.", ["offer"]),
            F("client_sig", "signature", "Client signature", ["offer"]),
            F("accept_date", "date", "Date", ["offer"], width="half")]},
        {"key": "disbursement", "name": "Disbursement", "fields": [
            F("disbursed_on", "date", "Disbursed on", ["disburse"], width="half"),
            F("credited_account", "text", "Credited account", ["disburse"], width="half"),
            F("disb_ref", "text", "Disbursement reference", ["disburse"], width="half"),
            F("first_repayment", "date", "First repayment", ["disburse"], width="half"),
            F("maturity", "date", "Maturity date", ["disburse"], width="half")]},
    ],
    "steps": [
        {"key": "applicant", "name": "Applicant", "recipient": {"type": "anyone"},
         "documents": {"application": "fill", "assessment": "hidden", "compliance": "hidden", "committee": "hidden",
                       "offer": "hidden", "disbursement": "hidden"}},
        {"key": "kyc", "name": "ID check refresh", "recipient": {"type": "group", "group": "Customer service"},
         "condition": when("ID check status", "neq", "Verified"),
         "documents": {"application": "fill", "assessment": "hidden", "compliance": "hidden", "committee": "hidden",
                       "offer": "hidden", "disbursement": "hidden"},
         "settings": {"sla": {"amount": 1, "unit": "business_days", "escalate": "notify"}}},
        {"key": "officer", "name": "Loan officer review", "recipient": {"type": "group", "group": "Loan officers"},
         "documents": {"application": "fill", "assessment": "fill", "compliance": "hidden", "committee": "hidden",
                       "offer": "hidden", "disbursement": "hidden"},
         "settings": {"recurrence": True, "sla": {"amount": 2, "unit": "business_days", "escalate": "notify"}}},
        {"key": "officer_route", "type": "branch", "name": "Officer recommendation", "lanes": [
            {"id": "declined", "label": "Declined by the officer", "when": when("Officer recommendation", "eq", "Decline"),
             "target": "end"},
            {"id": "more_info", "label": "More information needed",
             "when": when("Officer recommendation", "eq", "Need more information"), "then": "officer",
             "steps": [{"key": "info", "name": "Customer: more information",
                        "recipient": {"type": "email", "email": "{field:Email}"},
                        "documents": {"application": "fill", "assessment": "hidden", "compliance": "hidden",
                                      "committee": "hidden", "offer": "hidden", "disbursement": "hidden"}}]}]},
        {"key": "risk_route", "type": "branch", "name": "Risk route", "lanes": [
            {"id": "committee", "label": "Large or higher-risk loan",
             "when": any_of(("Offered amount", "gt", "50000"), ("Credit band", "eq", "C"), ("Credit band", "eq", "D"),
                            ("Debt to income (%)", "gt", "40")),
             "then": "merge",
             "steps": [
                 {"key": "aml", "name": "Compliance screening", "recipient": {"type": "group", "group": "Compliance"},
                  "condition": any_of(("Credit band", "eq", "D"), ("Defaults in 5 years", "gt", "0"),
                                      ("Offered amount", "gt", "100000")),
                  "documents": {"application": "view", "assessment": "view", "compliance": "fill", "committee": "hidden",
                                "offer": "hidden", "disbursement": "hidden"}},
                 {"key": "committee", "name": "Credit committee", "recipient": {"type": "group", "group": "Credit committee"},
                  "documents": {"application": "view", "assessment": "view", "compliance": "view", "committee": "fill",
                                "offer": "hidden", "disbursement": "hidden"},
                  "settings": {"sla": {"amount": 3, "unit": "business_days", "escalate": "notify"}}}]}]},
        {"key": "committee_route", "type": "branch", "name": "Committee outcome", "lanes": [
            {"id": "committee_declined", "label": "Declined by the committee",
             "when": when("Committee decision", "eq", "Declined"), "target": "end"}]},
        {"key": "offer", "name": "Client signs the offer", "recipient": {"type": "email", "email": "{field:Email}"},
         "documents": {"application": "view", "assessment": "hidden", "compliance": "hidden", "committee": "hidden",
                       "offer": "fill", "disbursement": "hidden"},
         "settings": {"verify": {"method": "email"}, "reminder": {"enabled": True, "every_days": 2, "max": 3}}},
        {"key": "disburse", "name": "Disbursement", "recipient": {"type": "group", "group": "Loan operations"},
         "documents": {"application": "view", "assessment": "view", "compliance": "hidden", "committee": "view",
                       "offer": "view", "disbursement": "fill"},
         "settings": {"sla": {"amount": 1, "unit": "business_days", "escalate": "notify"}}},
    ],
    "bots": [
        {"type": "rename_packet", "name": "Name the application", "trigger": "step_complete", "step": "applicant",
         "config": {"pattern": "Loan – {field:Full name} – {field:Loan type}"}},
        {"type": "add_tags", "name": "Tag declined", "trigger": "flow_complete",
         "when": any_of(("Officer recommendation", "eq", "Decline"), ("Committee decision", "eq", "Declined")),
         "config": {"tags": "#Declined"}},
        {"type": "add_tags", "name": "Tag funded", "trigger": "step_complete", "step": "disburse", "config": {"tags": "#Funded"}},
        {"type": "add_tags", "name": "Tag committee", "trigger": "step_complete", "step": "committee", "config": {"tags": "#Committee"}},
        {"type": "email_custom", "name": "Decline letter", "trigger": "flow_complete",
         "when": any_of(("Officer recommendation", "eq", "Decline"), ("Committee decision", "eq", "Declined")),
         "config": {"to": "{field:Email}", "subject": "Your loan application",
                    "body": "Dear {field:Full name},\n\nThank you for applying for a {field:Loan type}. After reviewing "
                            "your application we are not able to offer you this loan at the moment. This decision is "
                            "based on our lending criteria and the information available to us.\n\nYou are welcome to "
                            "speak with your relationship manager about other options.\n\nKind regards,\nPersonal Lending"}},
        {"type": "email_on_complete", "name": "Send the signed loan pack", "trigger": "step_complete", "step": "disburse",
         "config": {"to": "{field:Email}", "subject": "Your loan has been paid out",
                    "body": "Dear {field:Full name},\n\nYour loan has been paid into account {field:Credited account}. "
                            "Your signed offer and loan documents are attached. Your first repayment is due on "
                            "{field:First repayment}.\n\nKind regards,\nPersonal Lending", "attach_pdf": "on"}},
    ],
}

# the start form: everything the applicant sees (not the ID-check expiry, account or segment, which stay internal)
FORM_FIELDS = [{k: v for k, v in f.items() if k != "filled_by"}
               for f in CUSTOMER_FIELDS if f["id"] not in ("kyc_expires", "current_account", "segment")] + \
              [{k: v for k, v in f.items() if k != "filled_by"} for f in LOAN_FIELDS]
FORM_LOOKUP_MAP = [("full_name", "full_name"), ("email", "email"), ("phone", "phone"), ("address", "address"),
                   ("employer", "employer"), ("job_title", "job_title"), ("monthly_income", "monthly_income"),
                   ("existing_monthly_debt", "existing_debt"), ("kyc_status", "kyc_status")]

# customer, loan type, amount, term, purpose, story
APPLICATIONS = [
    ("CUS-10003", "Car loan", "20000", "48", "A reliable family car to replace our 14-year-old one.", "funded", 19),
    ("CUS-10019", "Personal loan", "25000", "60", "Wedding costs and setting up our first home.", "awaiting_disbursement", 9),
    ("CUS-10007", "Home improvement", "85000", "84", "New roof and solar panels for the house.", "awaiting_signature", 12),
    ("CUS-10022", "Debt consolidation", "15000", "48", "Combine two loans into one lower monthly payment.", "at_committee", 7),
    ("CUS-10034", "Personal loan", "8000", "36", "Medical bills after an operation.", "at_compliance", 5),
    ("CUS-10032", "Car loan", "12000", "48", "A car to get to a new job.", "declined_officer", 10),
    ("CUS-10026", "Home improvement", "60000", "84", "Kitchen and bathroom renovation.", "declined_committee", 14),
    ("CUS-10012", "Personal loan", "9000", "36", "Furniture for a new apartment.", "more_info", 4),
    ("CUS-10036", "Car loan", "30000", "60", "Upgrade to an electric car.", "id_check", 2),
    ("CUS-10015", "Debt consolidation", "40000", "72", "Pay off a credit card and a car loan.", "submitted", 1),
]


def step_names():
    """{blueprint step key: step name}, lanes included."""
    out = {}

    def walk(steps):
        for s in steps:
            if s.get("type") == "branch":
                for lane in s["lanes"]:
                    walk(lane.get("steps", []))
            else:
                out[s["key"]] = s["name"]
    walk(BLUEPRINT["steps"])
    return out


class Command(BaseCommand):
    help = "Add the bank loan example: a SQLite core-banking connector, the Personal loan workflow, its start form and applications."

    def add_arguments(self, parser):
        parser.add_argument("--workspace", default="demo")
        parser.add_argument("--owner", default="owner")
        parser.add_argument("--replace", action="store_true")

    def handle(self, *args, workspace, owner, replace=False, **opts):
        ws = Workspace.objects.filter(slug=workspace).first()
        if not ws:
            raise CommandError(f"No workspace {workspace!r} — run seed_demo first.")
        self.owner = User.objects.filter(username=owner).first() or User.objects.filter(email__iexact=owner).first()
        if not self.owner:
            raise CommandError(f"No user {owner!r}.")
        try:
            connectors.database_path(DATABASE)
        except connectors.ConnectorError as exc:
            raise CommandError(f"{exc} Run: python examples/bank-loan/make_bank_db.py")
        existing = Workflow.objects.filter(workspace=ws, name=WF_NAME)
        if existing.exists():
            if not replace:
                self.stdout.write("The bank example already exists (pass --replace to rebuild it).")
                return
            Packet.objects.filter(workflow__in=existing).delete()
            IntakeForm.objects.filter(workspace=ws, name=FORM_NAME).delete()
            existing.delete()
            Connector.objects.filter(workspace=ws, name=CONNECTOR_NAME).delete()
        self.ws, self.rng = ws, random.Random(42)
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"), transaction.atomic():
            self.people = self._team()
            self.conn = self._connector()
            wf = self._workflow()
            form = self._form(wf)
            count = self._applications(wf)
        self.stdout.write(self.style.SUCCESS(
            f"Bank example ready: workflow “{wf.name}” (id {wf.pk}), form “{form.name}” (/form/{form.token}/ if published), "
            f"connector “{self.conn.name}”, {count} applications."))

    # ------------------------------------------------------------------ setup
    def _team(self):
        import os

        password = os.environ.get("PAPERLANE_DEMO_PASSWORD", "paperlane-demo-2026")
        people = {u.username: u for u in User.objects.filter(username__in=["owner", "sam", "ana", "cal", "fay"])}
        for uname, first, last, position, groups in TEAM:
            u, created = User.objects.get_or_create(username=uname, defaults={
                "email": f"{uname}@paperlane.test", "first_name": first, "last_name": last})
            if created:
                u.set_password(password)
                u.save()
            Profile.objects.get_or_create(user=u)
            m, _ = Membership.objects.get_or_create(user=u, workspace=self.ws, defaults={"role": Membership.FILLER, "position": position})
            for g in groups:
                Group.objects.get_or_create(workspace=self.ws, name=g)[0].members.add(m)
            people[uname] = u
        for g, names in DEMO_MEMBERS.items():
            grp = Group.objects.get_or_create(workspace=self.ws, name=g)[0]
            for n in names:
                m = Membership.objects.filter(workspace=self.ws, user__username=n).first()
                if m:
                    grp.members.add(m)
        return people

    def _connector(self):
        c = Connector.objects.create(
            workspace=self.ws, name=CONNECTOR_NAME, kind=Connector.KIND_SQLITE, database=DATABASE, created_by=self.owner,
            description="Fictional core-banking data: customers, accounts, loans and credit-bureau records (read-only).",
            actions=[
                {"key": "customer", "name": "Find customer", "sql": CUSTOMER_SQL, "map": []},
                {"key": "credit", "name": "Credit check", "sql": CREDIT_SQL, "map": [
                    {"field": "Credit score", "path": "score"}, {"field": "Credit band", "path": "band"},
                    {"field": "Defaults in 5 years", "path": "defaults_5y"},
                    {"field": "Most days past due", "path": "max_days_past_due"},
                    {"field": "Credit enquiries (6 months)", "path": "enquiries_6m"},
                    {"field": "Average balance (6 months)", "path": "avg_balance_6m"}]}])
        return c

    def _workflow(self):
        import copy

        spec = copy.deepcopy(BLUEPRINT)
        spec["bots"].insert(0, {"type": "connector_action", "name": "Credit check (core banking)", "trigger": "step_complete",
                                "step": "applicant", "config": {"connector": str(self.conn.pk), "action": "credit"}})
        wf, problems = build_workflow(self.ws, self.owner, spec, publish=True, strict=False)
        if problems:
            self.stdout.write(f"Builder notes: {problems}")
        ids = {f["id"] for d in wf.documents.all() for f in d.fields}
        o = wf.options or {}
        o["key_dates"] = [{"field": "first_repayment", "label": "First repayment", "remind": [7, 1], "emails": ""},
                          {"field": "maturity", "label": "Loan matures", "remind": [60, 30, 0], "emails": ""}]
        o["key_dates"] = [k for k in o["key_dates"] if k["field"] in ids]
        o["duplicates"] = {"fields": ["Customer number"], "days": 30}
        wf.options = o
        wf.save(update_fields=["options"])
        return wf

    def _form(self, wf):
        spec = {"name": FORM_NAME, "workflow_id": wf.pk, "publish": True,
                "description": "Existing customers: your customer number and date of birth fill in your details from your account.",
                "fields": [{"id": "h", "type": "heading", "label": "Apply for a personal loan",
                            "text": "Enter your customer number and date of birth, and we fill in your details from your account."}] + FORM_FIELDS,
                "lookups": [{"name": "Find the customer in core banking", "trigger": "dob", "source": "connector",
                             "connector": self.conn.pk, "action": "customer", "min_length": 10,
                             "map": [{"from": a, "to": b} for a, b in FORM_LOOKUP_MAP],
                             "found_message": "Welcome back, your details are filled in from your account.",
                             "not_found_message": "We couldn't find that customer number. Check it and try again."}],
                "options": {"submit_label": "Submit my application", "complete_first_step": True,
                            "recipient_email_field": "email", "recipient_name_field": "full_name",
                            "document_name": "Loan – {field:Full name} – {field:Loan type}",
                            "success_title": "Application received",
                            "success_body": "Thank you. A loan officer will review your application within two business days."}}
        return build_form(self.ws, self.owner, spec)

    # ------------------------------------------------------------------ applications
    def _customer(self, cid):
        cols, rows = connectors.query_sqlite(self.conn, SEED_SQL, {"@value": cid})
        if not rows:
            raise CommandError(f"Customer {cid} is not in {DATABASE}.")
        r = rows[0]
        return {"Customer number": r["customer_id"], "Date of birth": r["date_of_birth"], "Full name": r["full_name"],
                "Email": r["email"], "Phone": r["phone"],
                "Address": r["address"], "Employer": r["employer"], "Job title": r["job_title"],
                "Monthly income": str(r["monthly_income"]), "Existing monthly debt": str(r["existing_monthly_debt"]),
                "ID check status": r["kyc_status"], "Current account": r["current_account"] or "", "Segment": r["segment"]}

    def _applications(self, wf):
        from flows.management.commands.seed_stories import _signature

        P = self.people
        for cid, product, amount, term, purpose, story, days_ago in APPLICATIONS:
            cust = self._customer(cid)
            app = {"product": product, "amount": amount, "term": term, "rate": "11.5", "purpose": purpose, "consent": True,
                   "app_sig": _signature(cust["Full name"], sum(map(ord, cid)))}
            plan = [("applicant", None, app)]
            if cust["ID check status"] != "Verified":
                plan.append(("kyc", P["kai"], {"kyc_status": "Verified", "kyc_expires": self._day(3 * 365)}))
            offer_amount = str(int(int(amount) * (0.95 if story == "awaiting_signature" else 1)))
            rate = {"A": "8.9", "B": "10.9", "C": "13.5", "D": "15.9"}
            officer = {"id_ok": ["Yes"], "income_ok": "Yes", "recommendation": "Approve", "offer_amount": offer_amount,
                       "officer_notes": "Payslips match the income on file; repayment history is clean."}
            if story == "declined_officer":
                officer = {"id_ok": ["Yes"], "income_ok": "Yes", "recommendation": "Decline",
                           "officer_notes": "Current loan 45 days in arrears and a default in the last 5 years."}
            if story == "more_info":
                officer = {"id_ok": ["Yes"], "income_ok": "No", "recommendation": "Need more information",
                           "info_request": "Please upload your last three payslips; the one we have is from June."}
            if story != "id_check" and story != "submitted":
                plan.append(("officer", P["lina"], officer))
            plan.append(("aml", P["ruth"], {"screening": "Clear", "aml_notes": "No sanctions or PEP matches."}))
            committee = {"committee_decision": "Declined" if story == "declined_committee" else "Approved",
                         "committee_notes": "Exposure too high for the income." if story == "declined_committee" else
                         "Approved; within appetite for the band."}
            if story not in ("at_committee", "at_compliance"):
                plan.append(("committee", P["omar"], committee))
            plan.append(("offer", None, {"accept": True, "client_sig": _signature(cust["Full name"], len(cid)),
                                         "accept_date": self._day(-max(days_ago - 3, 0))}))
            if story == "funded":
                first = self._day(30 - days_ago + 5)
                plan.append(("disburse", P["priya"], {
                    "disbursed_on": self._day(-days_ago + 6), "credited_account": cust["Current account"],
                    "disb_ref": f"DSB-{cid[4:]}-{self.rng.randint(100, 999)}", "first_repayment": first,
                    "maturity": self._day(30 - days_ago + 5 + int(term) * 30)}))
            stop = {"funded": None, "awaiting_disbursement": "disburse", "awaiting_signature": "offer",
                    "at_committee": "committee", "at_compliance": "aml", "declined_officer": None, "declined_committee": None,
                    "more_info": "info", "id_check": "kyc", "submitted": "officer"}[story]
            self._play(wf, cust, plan, stop, days_ago, rate)
        return len(APPLICATIONS)

    def _play(self, wf, cust, plan, stop_at, days_ago, rates):
        """Run the steps in order, as each step's real holder, until the document waits at ``stop_at``."""
        rng = self.rng
        t = timezone.now() - dt.timedelta(days=days_ago, hours=rng.randint(1, 6))
        t = min(t, timezone.now() - dt.timedelta(hours=3))
        first = {"email": cust["Email"], "name": cust["Full name"], "user": None}
        mark = AuditEvent.objects.order_by("-id").values_list("id", flat=True).first() or 0
        p = engine.start_packet(wf, source="form", prefill=cust, first_assignee=first,
                                name=f"Loan – {cust['Full name']}")
        Packet.objects.filter(pk=p.pk).update(created_at=t)
        timeline = [(mark, t)]
        names = step_names()  # the builder gives steps new keys, so runs are matched by step name
        by_name = {names[k]: v for k, _, v in plan}
        users = {names[k]: u for k, u, _ in plan}
        for _ in range(12):
            run = p.runs.filter(status__in=[StepRun.PENDING, StepRun.OPENED]).order_by("id").first()
            if not run or (stop_at and run.step_name == names[stop_at]) or run.step_name not in by_name:
                break
            values = dict(by_name[run.step_name])
            if run.step_name == names["officer"] and values.get("recommendation") == "Approve":
                band = str(p.flat_values().get("Credit band") or "B")
                values["offer_rate"] = rates.get(band, "11.5")
            opened = t + dt.timedelta(minutes=rng.randint(30, 240))
            done = min(opened + dt.timedelta(minutes=rng.randint(8, 50)), timezone.now() - dt.timedelta(minutes=30))
            StepRun.objects.filter(pk=run.pk).update(created_at=t, opened_at=opened)
            mark = AuditEvent.objects.order_by("-id").values_list("id", flat=True).first() or 0
            by_doc = {d["key"]: {k: v for k, v in values.items() if k in {f["id"] for f in d["fields"]}} for d in p.documents}
            user = users.get(run.step_name)
            if user is not None and run.assignee_group_id:
                run.claim(user)
            engine.complete_run(run, by_doc, user=user)
            StepRun.objects.filter(pk=run.pk).update(completed_at=done)
            timeline.append((mark, done))
            p.refresh_from_db()
            t = min(done + dt.timedelta(hours=rng.randint(3, 40)), timezone.now() - dt.timedelta(minutes=20))
        nxt = p.runs.filter(status__in=[StepRun.PENDING, StepRun.OPENED]).order_by("id").first()
        if nxt:
            StepRun.objects.filter(pk=nxt.pk).update(created_at=timeline[-1][1])
        for i, (start_id, at) in enumerate(timeline):
            qs = AuditEvent.objects.filter(packet=p, id__gt=start_id)
            if i + 1 < len(timeline):
                qs = qs.filter(id__lte=timeline[i + 1][0])
            qs.update(created_at=at)
        last = timeline[-1][1]
        p.refresh_from_db()
        Packet.objects.filter(pk=p.pk).update(last_activity_at=last, **({"completed_at": last} if p.completed_at else {}))
        if p.completed_at:
            from flows import keydates
            from flows.sealing import reseal_backdated

            reseal_backdated(p)  # the seal covers completion times; re-take it after back-dating
            keydates.capture(Packet.objects.get(pk=p.pk))

    @staticmethod
    def _day(offset):
        return (timezone.localdate() + dt.timedelta(days=offset)).isoformat()
