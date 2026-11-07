"""Ready-made start forms (Forms › New form › templates) and the demo "Policies" spreadsheet their lookups use.

Field names match the demo workflows (Insurance Claim, Beneficiary change request) so values flow straight into them.
"""

POLICY_SHEET = "Policies"
POLICY_COLUMNS = ["PolicyNumber", "InsuredsName", "PolicyType", "Email", "Phone", "Branch", "Address"]
# fictional sample data for trying lookups
POLICY_ROWS = [
    ["POL-100001", "Alex Example", "Life", "alex@example.com", "+1 868 555 0101", "Head office", "12 Sample Street, Riverton"],
    ["POL-100002", "Jordan Sample", "Investment", "jordan@example.com", "+1 868 555 0102", "Central", "4 Demo Avenue, Millbrook"],
    ["POL-100003", "Riley Demo", "Life", "riley@example.com", "+1 868 555 0103", "South", "9 Test Road, Southport"],
    ["POL-100004", "Casey Placeholder", "Annuity", "casey@example.com", "+1 868 555 0104", "North", "21 Mock Lane, Northfield"],
    ["POL-100005", "Morgan Fictional", "Investment", "morgan@example.com", "+1 868 555 0105", "East", "3 Imaginary Bay, Eastbay"],
]
BRANCHES = ["Head office", "North", "Central", "South", "East"]


def f(fid, type_, label, required=False, **extra):
    return {"id": fid, "type": type_, "label": label, "name": extra.pop("name", label), "required": required,
            "role": "anyone", **extra}


def show_if(field_id, value):
    return {"logic": "and", "rules": [{"field": field_id, "op": "eq", "value": value}]}


def policy_lookup(trigger="policy", mapping=None, name="Look up policy"):
    """In-form bot: fill the policyholder's details from the Policies spreadsheet."""
    return {"id": "lk_policy", "name": name, "trigger": trigger, "source": "sheet", "sheet": POLICY_SHEET,
            "key_column": "PolicyNumber", "min_length": 6,
            "map": mapping or [{"from": "InsuredsName", "to": "insured"}, {"from": "Email", "to": "email"},
                               {"from": "Phone", "to": "phone"}, {"from": "PolicyType", "to": "ptype"}],
            "found_message": "Policy found — details filled in.", "not_found_message": "No policy with that number."}


TEMPLATES = {
    "claim_intake": {
        "name": "Insurance claim intake", "category": "Insurance",
        "description": "Policy lookup fills the policyholder's details; claim details, documents and signature.",
        "fields": [
            f("h", "heading", "Submit a claim", text="Enter your policy number — we'll fill in your details."),
            f("policy", "text", "Policy number", True, width="half", placeholder="POL-100001", name="PolicyNumber",
              pattern=r"POL-\d{6}", pattern_message="Use the format POL-123456."),
            f("ptype", "text", "Policy type", width="half", name="PolicyType", readonly=True),
            f("insured", "text", "Full name", True, width="half", name="Full name"),
            f("email", "email", "Email", True, width="half", name="Email"),
            f("phone", "phone", "Phone", width="half", name="Phone"),
            f("ctype", "radio", "Claim type", True, options=["Medical", "Property", "Vehicle"], name="Claim type"),
            f("date", "date", "Incident date", True, width="half", name="Incident date"),
            f("amount", "currency", "Claimed amount", True, width="half", min=1, name="Claimed amount"),
            f("vehicle", "text", "Vehicle registration", True, condition=show_if("ctype", "Vehicle"), name="Vehicle registration"),
            f("hospital", "text", "Hospital or clinic", True, condition=show_if("ctype", "Medical"), name="Hospital or clinic"),
            f("desc", "textarea", "What happened?", True, name="What happened?"),
            f("docs", "attachment", "Supporting documents", True, max_files=5, max_mb=20, name="Supporting documents"),
            f("sig", "signature", "Signature", True, name="Claimant signature"),
        ],
        "lookups": [policy_lookup()],
        "options": {"submit_label": "Submit claim", "success_title": "Claim received",
                    "success_body": "Thank you — your claim has been received and an assessor will review it.",
                    "recipient_email_field": "email", "recipient_name_field": "insured",
                    "document_name": "Claim {field:PolicyNumber} – {field:Full name}"},
        "workflow_hint": "Insurance Claim",
    },
    "beneficiary_change": {
        "name": "Beneficiary change request", "category": "Insurance",
        "description": "Front-office intake for a beneficiary change; the policy lookup fills the insured and branch.",
        "fields": [
            f("h", "heading", "Beneficiary change request", text="Front office intake"),
            f("policy", "text", "Policy number", True, width="half", name="PolicyNumber", placeholder="POL-100001"),
            f("insured", "text", "Insured's name", True, width="half", name="InsuredsName"),
            f("ptype", "text", "Policy type", width="half", name="PolicyType"),
            f("rdate", "date", "Request date", True, width="half", name="RequestDate"),
            f("branch", "dropdown", "Request branch", True, options=BRANCHES, width="half", name="RequestBranch"),
            f("pbranch", "dropdown", "Preferred branch", options=BRANCHES, width="half", name="PreferredBranch"),
            f("csr", "text", "CSR", width="half", name="CSR"),
            f("csremail", "email", "CSR email", width="half", name="CSREmail"),
            f("request_form", "attachment", "Signed request form", True, max_files=5, max_mb=20, name="SignedRequestForm"),
            f("subdocs", "attachment", "Submitted documents", True, max_files=10, max_mb=20, name="SubmittedDocuments"),
            f("hb1", "heading", "Beneficiary 1"),
            f("rtype", "dropdown", "Request type", True, options=["Add", "Change", "Remove"], width="half", name="RequestType"),
            f("b1first", "text", "First name", True, width="half", name="BeneficiarysFirstName"),
            f("b1last", "text", "Last name", True, width="half", name="BeneficiarysLastName"),
            f("b1dob", "date", "Date of birth", width="half", name="BeneficiarysDateOfBirth"),
            f("b1rel", "text", "Relationship", width="half", name="RelationshipOfBeneficiary"),
            f("b1pct", "number", "Applicable percentage", True, min=0, max=100, width="half", name="ApplicablePercentage"),
            f("hb2", "heading", "Beneficiary 2 (optional)"),
            f("rtype2", "dropdown", "Request type", options=["Add", "Change", "Remove"], width="half", name="RequestType2"),
            f("b2first", "text", "First name", width="half", name="BeneficiarysFirstName2"),
            f("b2last", "text", "Last name", width="half", name="BeneficiarysLastName2"),
            f("b2pct", "number", "Applicable percentage", min=0, max=100, width="half", name="ApplicablePercentage2"),
            f("total", "formula", "Total percentage", formula="{ApplicablePercentage} + {ApplicablePercentage2}", width="half"),
        ],
        "lookups": [policy_lookup(mapping=[{"from": "InsuredsName", "to": "insured"}, {"from": "PolicyType", "to": "ptype"},
                                           {"from": "Branch", "to": "branch"}])],
        "options": {"submit_label": "Submit request", "success_title": "Request submitted",
                    "success_body": "The request was sent to the branch's front office.",
                    "document_name": "Beneficiary Change – {field:PolicyNumber} – {field:InsuredsName}"},
        "workflow_hint": "Beneficiary Change",
    },
    "address_change": {
        "name": "Change of address", "category": "Policy service",
        "description": "Looks up the policy, shows the address on file and collects the new one with proof.",
        "fields": [
            f("h", "heading", "Change of address"),
            f("policy", "text", "Policy number", True, width="half", name="PolicyNumber", placeholder="POL-100001"),
            f("insured", "text", "Policyholder", width="half", name="InsuredsName", readonly=True),
            f("current", "textarea", "Address on file", name="Current address", readonly=True),
            f("newaddr", "address", "New address", True, name="New address"),
            f("eff", "date", "Effective from", True, width="half", name="Effective date"),
            f("proof", "attachment", "Proof of address", True, max_files=2, max_mb=10, name="Proof of address"),
            f("email", "email", "Email for confirmation", True, width="half", name="Email"),
            f("sig", "signature", "Signature", True, name="Signature"),
        ],
        "lookups": [policy_lookup(mapping=[{"from": "InsuredsName", "to": "insured"}, {"from": "Address", "to": "current"},
                                           {"from": "Email", "to": "email"}])],
        "options": {"submit_label": "Update my address", "success_title": "Address change received",
                    "recipient_email_field": "email", "recipient_name_field": "insured"},
    },
    "surrender_request": {
        "name": "Policy surrender request", "category": "Policy service",
        "description": "Full or partial surrender with payment details; the policy lookup fills the holder.",
        "fields": [
            f("h", "heading", "Surrender request"),
            f("policy", "text", "Policy number", True, width="half", name="PolicyNumber", placeholder="POL-100001"),
            f("insured", "text", "Policyholder", True, width="half", name="InsuredsName"),
            f("stype", "radio", "Surrender type", True, options=["Full surrender", "Partial withdrawal"], name="Surrender type"),
            f("amt", "currency", "Amount requested", True, condition=show_if("stype", "Partial withdrawal"), name="Amount requested"),
            f("pay", "dropdown", "Payment method", True, options=["Bank transfer", "Cheque"], width="half", name="Payment method"),
            f("acct", "text", "Bank account number", True, width="half", condition=show_if("pay", "Bank transfer"), name="Bank account"),
            f("reason", "textarea", "Reason (optional)", name="Reason"),
            f("email", "email", "Email", True, width="half", name="Email"),
            f("id", "attachment", "Photo ID", True, max_files=2, max_mb=10, name="Photo ID"),
            f("sig", "signature", "Signature", True, name="Signature"),
        ],
        "lookups": [policy_lookup(mapping=[{"from": "InsuredsName", "to": "insured"}, {"from": "Email", "to": "email"}])],
        "options": {"submit_label": "Submit request", "recipient_email_field": "email", "recipient_name_field": "insured"},
    },
    "service_request": {
        "name": "General service request", "category": "General",
        "description": "Contact details, request category and message. Routes any customer request into a workflow.",
        "fields": [
            f("h", "heading", "How can we help?"),
            f("name", "full_name", "Your name", True, name="Name"),
            f("email", "email", "Email", True, width="half", name="Email"),
            f("phone", "phone", "Phone", width="half", name="Phone"),
            f("policy", "text", "Policy number (if any)", width="half", name="PolicyNumber"),
            f("cat", "dropdown", "Request", True, width="half", name="Request category",
              options=["Policy information", "Payment question", "Update my details", "Complaint", "Other"]),
            f("msg", "textarea", "Message", True, name="Message"),
            f("att", "attachment", "Attachments", max_files=3, max_mb=10, name="Attachments"),
        ],
        "lookups": [],
        "options": {"submit_label": "Send request", "success_title": "Thanks — we got your request",
                    "recipient_email_field": "email"},
    },
    "employee_onboarding": {
        "name": "New employee details", "category": "HR",
        "description": "Personal details, start date, department and emergency contact for HR onboarding.",
        "fields": [
            f("h", "heading", "Welcome aboard"),
            f("person", "personal_info", "Personal information", True, name="Personal information"),
            f("email", "email", "Personal email", True, width="half", name="Email"),
            f("phone", "phone", "Phone", width="half", name="Phone"),
            f("addr", "address", "Home address", True, name="Home address"),
            f("start", "date", "Start date", True, width="half", name="Start date"),
            f("dept", "dropdown", "Department", True, width="half", name="Department",
              options=["Operations", "Finance", "Sales", "IT", "HR"]),
            f("ecname", "text", "Emergency contact", True, width="half", name="Emergency contact"),
            f("ecphone", "phone", "Emergency phone", True, width="half", name="Emergency phone"),
            f("sig", "signature", "Signature", True, name="Signature"),
        ],
        "lookups": [],
        "options": {"submit_label": "Submit details", "recipient_email_field": "email"},
    },
}


def ensure_policy_sheet(workspace):
    """Create the demo Policies spreadsheet (fictional rows) the template lookups read, if it is missing."""
    from .models import DataSheet, DataSheetRow

    sheet, created = DataSheet.objects.get_or_create(workspace=workspace, name=POLICY_SHEET,
                                                     defaults={"columns": POLICY_COLUMNS})
    if created or not sheet.rows.exists():
        DataSheetRow.objects.bulk_create([DataSheetRow(sheet=sheet, values=dict(zip(POLICY_COLUMNS, r))) for r in POLICY_ROWS])
    return sheet


def ensure_lookup_sheets(workspace, lookups):
    """Create the sample spreadsheets that template lookups read, if missing."""
    names = {lk.get("sheet") for lk in lookups or [] if lk.get("source") == "sheet"}
    if POLICY_SHEET in names:
        ensure_policy_sheet(workspace)
