"""Starter sections for the editor's field library (workspaces add their own as FieldBlock)."""


def _f(id, type, label, **kw):
    return {"id": id, "type": type, "label": label, "name": "" if type in ("heading", "description") else label, **kw}


STARTERS = [
    {"key": "bank", "name": "Bank details (direct deposit)", "description": "Payment method with bank fields shown only for direct deposit.",
     "fields": [_f("h", "heading", "Payment", text="How should we pay you?"),
                _f("pm", "radio", "Payment method", options=["Cheque", "Direct deposit"], required=True),
                _f("bn", "text", "Bank name", required=True, width="half", condition={"logic": "and", "rules": [{"field": "pm", "op": "eq", "value": "Direct deposit"}]}),
                _f("br", "text", "Branch", width="half", condition={"logic": "and", "rules": [{"field": "pm", "op": "eq", "value": "Direct deposit"}]}),
                _f("an", "text", "Account number", required=True, sensitive=True, width="half", condition={"logic": "and", "rules": [{"field": "pm", "op": "eq", "value": "Direct deposit"}]}),
                _f("at", "radio", "Account type", options=["Chequing", "Savings"], width="half", condition={"logic": "and", "rules": [{"field": "pm", "op": "eq", "value": "Direct deposit"}]})]},
    {"key": "contact", "name": "Contact details", "description": "Name, email and phone.",
     "fields": [_f("h", "heading", "Contact details"), _f("n", "full_name", "Full name", required=True),
                _f("e", "email", "Email", required=True, width="half"), _f("p", "phone", "Phone", width="half")]},
    {"key": "declaration", "name": "Declaration & signature", "description": "Truth declaration, signature and date.",
     "fields": [_f("h", "heading", "Declaration"),
                _f("c", "clickwrap", "Declaration", text="I confirm the information I have given is true and complete.", required=True),
                _f("s", "signature", "Signature", required=True, width="half"), _f("d", "date", "Date signed", required=True, width="half")]},
    {"key": "beneficiaries", "name": "Beneficiaries", "description": "Table of beneficiaries with relationship and share.",
     "fields": [_f("h", "heading", "Beneficiaries", text="Shares should add up to 100%."),
                _f("t", "table", "Beneficiaries", columns=["Full name", "Relationship", "Date of birth", "Share %"], min_rows=3)]},
    {"key": "address", "name": "Address with proof", "description": "Address, effective date and proof of address upload.",
     "fields": [_f("h", "heading", "Address"), _f("a", "address", "Address", required=True),
                _f("ef", "date", "Effective from", width="half"),
                _f("pr", "attachment", "Proof of address (utility bill or bank statement)", required=True)]},
]
