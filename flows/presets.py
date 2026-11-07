"""Ready-made workflow automations shown on the dashboard ("Popular workflow automations")."""
from django.db import transaction

from .models import Bot, Step, Workflow, WorkflowDocument
from .preset_blueprints import COMPLEX


def f(fid, type_, label, required=False, role="anyone", **extra):
    return {"id": fid, "type": type_, "label": label, "name": label, "required": required, "role": role, **extra}


PRESETS = {
    **COMPLEX,
    "employee_onboarding": {
        "name": "Employee onboarding",
        "description": "Collect new employee information automatically.",
        "icons": ["sheet", "doc"],
        "documents": [{"name": "New hire form", "fields": [
            f("h1", "heading", "New employee information"),
            f("full_name", "full_name", "Full name", True, "s1"),
            f("email", "email", "Personal email", True, "s1"),
            f("phone", "phone", "Phone", False, "s1"),
            f("address", "address", "Home address", True, "s1"),
            f("start", "date", "Start date", True, "s2"),
            f("dept", "dropdown", "Department", True, "s2", options=["Operations", "Finance", "Sales", "IT", "HR"]),
            f("sig", "signature", "Employee signature", True, "s1"),
            f("hr_sig", "signature", "HR approval", True, "s2"),
        ]}],
        "steps": [("s1", "New employee"), ("s2", "HR manager")],
        "bots": [("export_datasheet", "Export to spreadsheet", "flow_complete", {"sheet": "Employees"})],
    },
    "leave_request": {
        "name": "Vacation or leave request",
        "description": "Collect approved requests automatically.",
        "icons": ["sheet", "drive"],
        "documents": [{"name": "Leave request", "fields": [
            f("h1", "heading", "Leave request"),
            f("employee", "text", "Employee name", True, "s1"),
            f("type", "radio", "Leave type", True, "s1", options=["Vacation", "Sick", "Personal", "Other"]),
            f("from", "date", "From", True, "s1"),
            f("to", "date", "To", True, "s1"),
            f("reason", "textarea", "Reason", False, "s1"),
            f("decision", "radio", "Decision", True, "s2", options=["Approved", "Rejected"]),
            f("mgr_sig", "signature", "Manager signature", True, "s2"),
        ]}],
        "steps": [("s1", "Employee"), ("s2", "Manager")],
        "bots": [("add_tags", "Tag decision", "flow_complete", {"tags": "#{field:Decision}"})],
    },
    "sales_invoice": {
        "name": "Sales invoice",
        "description": "Create and approve sales invoices before sending them to customers.",
        "icons": ["cloud", "cloud"],
        "documents": [{"name": "Invoice", "fields": [
            f("h1", "heading", "Invoice"),
            f("customer", "text", "Customer", True, "s1"),
            f("cust_email", "email", "Customer email", True, "s1"),
            f("items", "table", "Line items", False, "s1", columns=["Item", "Qty", "Price"]),
            f("subtotal", "currency", "Subtotal", True, "s1"),
            f("tax", "currency", "Tax", False, "s1"),
            f("total", "formula", "Total", False, "s1", formula="{Subtotal} + {Tax}"),
            f("approve", "checkbox", "Approval", True, "s2", options=["Approved for sending"]),
        ]}],
        "steps": [("s1", "Sales rep"), ("s2", "Finance approver")],
        "bots": [("email_on_complete", "Email invoice to customer", "flow_complete",
                  {"to": "{field:Customer email}", "subject": "Your invoice", "attach_pdf": True})],
    },
    "purchase_request": {
        "name": "Item purchase request",
        "description": "Manage product purchase requests.",
        "icons": ["sheet", "doc"],
        "documents": [{"name": "Purchase request", "fields": [
            f("requester", "text", "Requester", True, "s1"),
            f("item", "text", "Item", True, "s1"),
            f("qty", "number", "Quantity", True, "s1"),
            f("price", "currency", "Unit price", True, "s1"),
            f("total", "formula", "Total cost", False, "s1", formula="{Quantity} * {Unit price}"),
            f("decision", "radio", "Decision", True, "s2", options=["Approved", "Rejected"]),
        ]}],
        "steps": [("s1", "Requester"), ("s2", "Approver")],
        "bots": [],
    },
    "marketing_campaign": {
        "name": "Marketing campaign",
        "description": "Review and approve new marketing campaigns.",
        "icons": ["sheet", "doc"],
        "documents": [{"name": "Campaign brief", "fields": [
            f("campaign", "text", "Campaign name", True, "s1"),
            f("channel", "checkbox", "Channels", True, "s1", options=["Email", "Social", "Print", "Radio", "TV"]),
            f("budget", "currency", "Budget", True, "s1"),
            f("brief", "textarea", "Brief", True, "s1"),
            f("assets", "attachment", "Creative assets", False, "s1"),
            f("rating", "rating", "Reviewer score", False, "s2"),
            f("decision", "radio", "Decision", True, "s2", options=["Approved", "Needs changes"]),
        ]}],
        "steps": [("s1", "Marketing"), ("s2", "Reviewer")],
        "bots": [],
    },
    "expense_approval": {
        "name": "Expense approval",
        "description": "Automate the internal submission of expense reports.",
        "icons": ["sheet", "doc"],
        "documents": [{"name": "Expense report", "fields": [
            f("employee", "text", "Employee", True, "s1"),
            f("expenses", "table", "Expenses", True, "s1", columns=["Date", "Description", "Amount"]),
            f("amount", "currency", "Total amount", True, "s1"),
            f("receipts", "attachment", "Receipts", False, "s1"),
            f("sig", "signature", "Employee signature", True, "s1"),
            f("decision", "radio", "Decision", True, "s2", options=["Approved", "Rejected"]),
        ]}],
        "steps": [("s1", "Employee"), ("s2", "Finance")],
        "branch": {"after": "s1", "label": "Large expense", "rules": [{"field": "Total amount", "op": "gt", "value": "1000"}],
                   "extra_step": ("s3", "Director")},
        "bots": [],
    },
}


@transaction.atomic
def create_from_preset(key, workspace, user):
    p = PRESETS[key]
    if p.get("blueprint"):
        from .blueprint import build_workflow

        wf, _problems = build_workflow(workspace, user, p["blueprint"])
        return wf
    wf = Workflow.objects.create(workspace=workspace, name=p["name"], description=p["description"], created_by=user)
    step_keys = {}
    order = 0
    for sk, name in p["steps"]:
        order += 10
        st = Step.objects.create(workflow=wf, order=order, name=name, recipient_type=Step.R_EMAIL)
        step_keys[sk] = st.key
        if p.get("branch", {}).get("after") == sk:
            br = p["branch"]
            extra_key, extra_name = br["extra_step"]
            order += 10
            branch = Step.objects.create(workflow=wf, order=order, kind=Step.BRANCH, name="Branches")
            order += 10
            extra = Step.objects.create(workflow=wf, order=order, name=extra_name, recipient_type=Step.R_EMAIL,
                                        condition={"logic": "and", "rules": br["rules"]})
            step_keys[extra_key] = extra.key
            branch.branches = [{"label": br["label"], "logic": "and", "rules": br["rules"], "target": extra.key}]
            branch.save()
    for i, d in enumerate(p["documents"]):
        fields = []
        for fld in d["fields"]:
            fld = dict(fld)
            fld["role"] = step_keys.get(fld.get("role"), "anyone")
            fields.append(fld)
        WorkflowDocument.objects.create(workflow=wf, name=d["name"], order=i, schema={"fields": fields})
    for bot_type, name, trigger, cfg in p.get("bots", []):
        Bot.objects.create(workflow=wf, bot_type=bot_type, name=name, trigger=trigger, config=cfg)
    return wf
