"""Bot catalog + executor.

Every bot listed in the catalog is executable. Bots that target third-party SaaS products
(Salesforce, Google Drive, Slack …) are delivered through the workspace's Connected accounts:
the payload is POSTed to the connection endpoint (e.g. a Zapier/Make/n8n hook or your own API),
and a local copy is always written so nothing is lost when no endpoint is configured.
"""
import csv
import io
import json
import os
import urllib.request

from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from accounts.models import Connection, UsageEvent

from . import mailer
from .conditions import evaluate
from .models import BotLog, DataSheet, DataSheetRow, Packet, Tag

# ---------------------------------------------------------------------------------
# config field helpers (rendered generically by templates/flows/bot_form.html)


def F(name, label, type="text", help="", options=None, required=False, placeholder=""):
    return {"name": name, "label": label, "type": type, "help": help, "options": options or [],
            "required": required, "placeholder": placeholder}


TOKENS_HELP = "Tokens: {document_name}, {workflow_name}, {today}, {now}, {field:Field name}"

INTEGRATIONS = [
    "Airtable", "AWS S3", "Basecamp", "Box", "Cisco Webex", "Citrix ShareFile", "DocuSign", "Dropbox", "Egnyte",
    "Formstack", "Gmail", "Google Calendar", "Google Drive", "Google Sheets", "HubSpot", "Jira", "Magento 1.x",
    "Magento 2.x", "Mailchimp", "Microsoft 365 Excel", "MS Dynamics 365", "MS SQL Server", "MS Teams", "MySQL",
    "NetSuite", "OneDrive", "Outlook", "Pipedrive", "PostgreSQL", "Procore", "QuickBooks", "Salesforce",
    "SendGrid", "ServiceNow", "SharePoint", "Slack", "Smartsheet", "Trello", "UiPath", "Zendesk", "Zoho CRM",
]
CLOUD_STORAGE = ["Dropbox", "Google Drive", "Box", "OneDrive", "SharePoint", "Egnyte", "Citrix ShareFile", "AWS S3"]

CATALOG = {
    # --- Automation · Documents
    "rename_packet": dict(name="Rename workflow instances on completion", category="Documents", free=True, popular=True,
                          desc="Rename a document package using field values after a recipient completes it.",
                          fields=[F("pattern", "New name", required=True, placeholder="{field:Full name} – {today}", help=TOKENS_HELP)]),
    "save_to_storage": dict(name="Save completed documents", category="Documents", free=True,
                            desc="Archive the completed documents as a PDF into a Paperlane storage folder.",
                            fields=[F("folder", "Folder", placeholder="Completed/{workflow_name}"),
                                    F("filename", "File name", placeholder="{document_name}", help=TOKENS_HELP)]),
    "lock_packet": dict(name="Lock documents", category="Permissions",
                        desc="Lock the document package so it can no longer be revised.", fields=[]),
    "archive_packet": dict(name="Archive documents", category="Documents",
                           desc="Move the document package to the Archive automatically.", fields=[]),
    "attach_certificate": dict(name="Generate certificate of completion", category="Documents",
                               desc="Store a certificate of completion with the audit trail in storage.",
                               fields=[F("folder", "Folder", placeholder="Certificates")]),
    # --- Automation · Email
    "email_on_complete": dict(name="Email on documents completion", category="Email", popular=True, free=True,
                              desc="Send an email notification when a recipient completes the documents.",
                              fields=[F("to", "Send to", required=True, placeholder="ops@example.com, {field:Email}"),
                                      F("subject", "Subject", placeholder="Documents have been completed"),
                                      F("body", "Message", "textarea", help=TOKENS_HELP),
                                      F("attach_pdf", "Attach documents as PDF", "checkbox")]),
    "route_to_step": dict(name="Send documents to recipients (routing)", category="Documents", popular=True, free=True,
                          desc="Flow-diagram routing: when its conditions match, send the document to a step with a custom notification.",
                          fields=[F("step", "Send to step", "step", required=True),
                                  F("recipient", "Recipient email (optional)", placeholder="{field:initiatoremail} or name@company.com",
                                    help="Leave empty to use the step's configured recipient."),
                                  F("group", "…or a group", "group"),
                                  F("subject", "Notification subject", placeholder="{workflow_name}"),
                                  F("body", "Notification message", "textarea", help=TOKENS_HELP),
                                  F("attach_pdf", "Attach documents to the email", "checkbox")]),
    "calculate": dict(name="Make calculations in documents", category="Fields data", popular=True, free=True,
                      desc="Calculate field values with formulas (+ − * / round, min, max, abs).",
                      fields=[F("mapping", "Calculations", "mapping", required=True,
                                help="One per line: Target field = expression, e.g. abs_value = {VarianceMAV} * -1")]),
    "remove_tags": dict(name="Delete tags", category="Tags", free=True,
                        desc="Remove tags from the document package.",
                        fields=[F("tags", "Tags to remove (comma separated)", required=True)]),
    "reassign_step": dict(name="Reassign role", category="Documents",
                          desc="Re-send a step to another recipient or group (e.g. when it is declined), optionally after a delay.",
                          fields=[F("step", "Step", "step", required=True), F("recipient", "Recipient email"),
                                  F("group", "…or a group", "group"), F("subject", "Notification subject"),
                                  F("body", "Notification message", "textarea")]),
    "technical_revision": dict(name="Technical revision bot", category="Documents",
                               desc="Save a technical revision of the document and optionally complete a step automatically.",
                               fields=[F("step", "Complete this step automatically (optional)", "step")]),
    "reminder_custom": dict(name="Send custom reminder", category="Reminder",
                            desc="Email a custom reminder to fixed addresses (use the bot delay for 'on delay' reminders).",
                            fields=[F("emails", "Send to", required=True), F("subject", "Subject", required=True),
                                    F("body", "Message", "textarea", required=True), F("with_link", "Include document link", "checkbox")]),
    "email_on_open": dict(name="Email on documents opening", category="Email", free=True,
                          desc="Send email notifications when a recipient opens the documents.",
                          fields=[F("to", "Send to", required=True)]),
    "email_custom": dict(name="Send custom email", category="Email",
                         desc="Send an email with your own subject and text.",
                         fields=[F("to", "Send to", required=True), F("subject", "Subject", required=True),
                                 F("body", "Message", "textarea", required=True, help=TOKENS_HELP)]),
    # --- Fax / SMS / Reminder
    "fax_send": dict(name="Send documents by fax", category="Fax",
                     desc="Deliver the PDF to an email-to-fax gateway (e.g. 18685551234@fax.provider.com).",
                     fields=[F("gateway_email", "Fax gateway address", required=True)]),
    "sms_send": dict(name="Send SMS notification", category="SMS",
                     desc="POST an SMS through a Twilio-compatible gateway URL from Connected accounts.",
                     fields=[F("to", "Phone number", required=True, placeholder="+1868… or {field:Phone}"),
                             F("message", "Message", "textarea", required=True, help=TOKENS_HELP),
                             F("connection", "Connection", "connection")]),
    "reminder": dict(name="Remind recipients to complete documents", category="Reminder", popular=True, free=True,
                     desc="Email reminders to recipients who have not completed their step yet.",
                     fields=[F("every_days", "Remind every (days)", "number", required=True, placeholder="2"),
                             F("max", "Maximum reminders", "number", placeholder="3")]),
    # --- Tags
    "add_tags": dict(name="Add tags to documents", category="Tags", free=True,
                     desc="Tag document packages to find them faster (supports conditions).",
                     fields=[F("tags", "Tags (comma separated)", required=True, placeholder="#Approved, {field:Type}")]),
    # --- Data movement · Fields data
    "prefill_custom": dict(name="Pre-fill with custom values", category="Fields data", free=True, popular=True,
                           desc="Pre-fill documents with custom data.",
                           fields=[F("mapping", "Field values", "mapping", help="One per line: Field name = value. " + TOKENS_HELP)]),
    "prefill_doc_to_doc": dict(name="Pre-fill from doc to doc", category="Fields data", free=True, popular=True,
                               desc="Pre-fill a document with data from another document or within the same document.",
                               fields=[F("mapping", "Copy fields", "mapping", help="One per line: Target field = Source field")]),
    "prefill_from_workflow": dict(name="Pre-fill from another workflow", category="Fields data",
                                  desc="Pre-fill documents with data from the latest matching document of another workflow.",
                                  fields=[F("workflow", "Source workflow", "workflow", required=True),
                                          F("match_field", "Match on field (optional)", placeholder="Policy number"),
                                          F("mapping", "Copy fields", "mapping", help="Target field = Source field (blank = same names)")]),
    "prefill_csv": dict(name="Pre-fill from CSV file", category="Fields data",
                        desc="Pre-fill documents with data from a CSV file (row matched by a key field).",
                        fields=[F("csv", "CSV content", "textarea", required=True, help="Header row must use field names."),
                                F("key_field", "Key field", required=True)]),
    "prefill_datasheet": dict(name="Pre-fill from a spreadsheet", category="Fields data",
                              desc="Look up a row in a Paperlane spreadsheet (filled by export bots) and pre-fill fields.",
                              fields=[F("sheet", "Spreadsheet", "datasheet", required=True), F("key_field", "Key field", required=True)]),
    "export_datasheet": dict(name="Export to spreadsheet", category="Fields data", popular=True, free=True,
                             desc="Append the document's field values as a row in a Paperlane spreadsheet (downloadable as CSV).",
                             fields=[F("sheet", "Spreadsheet name", required=True, placeholder="Submissions"),
                                     F("fields", "Fields (comma separated, blank = all)")]),
    "autofill_signature": dict(name="Auto-fill sender signature", category="Signatures",
                               desc="Fill signature fields with the sender's default signature.",
                               fields=[F("field", "Signature field", required=True)]),
    # --- Webhook
    "webhook": dict(name="Send data via webhook", category="Webhook", popular=True,
                    desc="POST the document data as JSON to any URL.",
                    fields=[F("url", "Webhook URL", "url", required=True), F("secret", "Signing secret (sent as X-Paperlane-Secret)")]),
    # --- Automation (added) -----------------------------------------------------------------------------------------
    "reference_number": dict(name="Generate a reference number", category="Fields data", popular=True, free=True,
                             default_trigger="flow_start",
                             desc="Give each document a unique, sequential reference such as WD-2026-00042.",
                             fields=[F("target", "Save into field", required=True, placeholder="Reference"),
                                     F("pattern", "Pattern", required=True, placeholder="WD-{yyyy}-{seq:5}",
                                       help="{seq:5} = counter padded to 5 digits · {yyyy} {yy} {mm} {dd} · {field:Name}"),
                                     F("counter", "Counter name", placeholder="withdrawals",
                                       help="Documents that share a counter name share the sequence. Default: this workflow."),
                                     F("yearly", "Restart the counter every year", "checkbox"),
                                     F("rename", "Also put it in front of the document name", "checkbox")]),
    "assign_round_robin": dict(name="Assign to the next team member", category="Documents", popular=True, free=True,
                               default_trigger="step_start",
                               desc="Hand a group step to one member automatically — taking turns, or whoever has the least open work.",
                               fields=[F("step", "Step (blank = the step that just started)", "step"),
                                       F("group", "Choose from group (blank = the step's group)", "group"),
                                       F("strategy", "How to choose", "select", options=[("least", "Least open work"), ("rotate", "Take turns")]),
                                       F("skip_away", "Skip people who are out of office", "checkbox")]),
    "transform_field": dict(name="Transform or combine fields", category="Fields data", popular=True, free=True,
                            desc="Upper/lower case, trim, combine text, add days or business days to a date, round numbers.",
                            fields=[F("target", "Save into field", required=True),
                                    F("operation", "Operation", "select", options=[
                                        ("template", "Combine text (template)"), ("upper", "UPPER CASE"), ("lower", "lower case"),
                                        ("title", "Title Case"), ("trim", "Trim spaces"), ("digits", "Keep digits only"),
                                        ("add_days", "Date + days"), ("add_business_days", "Date + business days"),
                                        ("round", "Round number"), ("today", "Today's date")]),
                                    F("source", "Source / template", placeholder="{field:First Name} {field:Last Name}",
                                      help="For case/trim/date/number operations: the source field name (or a {field:…} template)."),
                                    F("amount", "Days / decimals", "number", placeholder="2")]),
    "start_workflow": dict(name="Start another workflow", category="Documents", popular=True, free=True,
                           desc="Chain processes: start a document of another workflow and copy values across.",
                           fields=[F("workflow", "Workflow to start", "workflow", required=True),
                                   F("recipient", "First recipient (blank = the workflow's own first step)", placeholder="{field:Email}"),
                                   F("name", "Document name", placeholder="{document_name} – payment"),
                                   F("mapping", "Copy values", "mapping", help="One per line: Field in the new document = {field:Field here}"),
                                   F("link_back", "Add a comment linking the two documents", "checkbox")]),
    "set_deadline": dict(name="Set a deadline on the step", category="Reminder", free=True, default_trigger="step_start",
                         desc="Give the open step a due date — from a date field or a number of hours/days from now.",
                         fields=[F("step", "Step (blank = every open step)", "step"),
                                 F("date_field", "Due on this date field (optional)", placeholder="Payment Date"),
                                 F("hours", "…or due in N hours", "number", placeholder="24")]),
    "post_chat": dict(name="Post to Teams / Slack / Google Chat", category="Webhook", popular=True, free=True,
                      desc="Send a message to a chat channel through its incoming-webhook URL.",
                      fields=[F("url", "Incoming webhook URL", "url", required=True),
                              F("message", "Message", "textarea", required=True,
                                placeholder="New withdrawal {document_name} for {field:Payout} — {link}", help=TOKENS_HELP + ", {link}")]),
    "add_comment": dict(name="Add a comment", category="Documents", free=True,
                        desc="Write an automatic note on the document (visible in the Comments tab).",
                        fields=[F("text", "Comment", "textarea", required=True, help=TOKENS_HELP),
                                F("author", "Shown as", placeholder="Automation")]),
    "notify_team": dict(name="Notify people in the app", category="Email", free=True,
                        desc="Send an in-app notification (bell) to teammates or a group, without email.",
                        fields=[F("to", "Teammates (emails, comma separated)", placeholder="lead@company.com"),
                                F("group", "…and/or a group", "group"),
                                F("title", "Title", required=True, placeholder="{document_name} needs attention"),
                                F("body", "Text", "textarea")]),
    "connector_action": dict(name="Run a custom connector", category="Webhook", popular=True, free=True,
                             desc="Call one of your custom connectors (any HTTP/JSON API) and write its answer into fields.",
                             fields=[F("connector", "Connector", "connector", required=True),
                                     F("action", "Action key", required=True, placeholder="lookup_policy",
                                       help="The action's key, as listed on the connector."),
                                     F("mapping", "Extra response mapping (optional)", "mapping",
                                       help="Field name = json.path — added to the action's own mapping.")]),
    "ai_fill_fields": dict(name="AI: fill fields", category="AI", popular=True, free=True,
                           desc="An AI model (local Ollama, OpenAI-compatible or Claude) fills fields from the document's data.",
                           fields=[F("targets", "Fields to fill", required=True, placeholder="Risk level, Next action",
                                     help="Comma separated field names."),
                                   F("instructions", "Instructions", "textarea",
                                     placeholder="Classify the request and suggest the next action.")]),
    "ai_summary": dict(name="AI: summarize the document", category="AI", free=True,
                       desc="Write a short summary into a field and/or as a comment for the next person.",
                       fields=[F("target", "Summary field (optional)", placeholder="Summary"),
                               F("add_comment", "Also add it as a comment", "checkbox"),
                               F("instructions", "Instructions", "textarea", placeholder="Focus on amounts, dates and risks.")]),
    "ai_classify": dict(name="AI: choose an option", category="AI", free=True,
                        desc="Pick the best option of a radio/dropdown field (e.g. priority or category) from the data.",
                        fields=[F("target", "Choice field", required=True, placeholder="Priority"),
                                F("instructions", "Instructions", "textarea", placeholder="High if the amount is over 50,000.")]),
    "ai_review": dict(name="AI: review against rules", category="AI", popular=True, free=True,
                      desc="Check the document against your rules in plain English. Writes Passed/Needs review into a field, "
                           "tags documents with problems and lists them in a comment.",
                      fields=[F("rules", "Rules", "textarea", required=True,
                                placeholder="The payout must not exceed the policy value.\nA bank account is required for transfers."),
                              F("target", "Result field (optional)", placeholder="Review result",
                                help="Gets “Passed” or “Needs review” (use those options for a dropdown/radio)."),
                              F("tag", "Tag when problems are found", placeholder="#Needs review"),
                              F("add_comment", "List the problems as a comment", "checkbox")]),
    "save_to_drive": dict(name="Save to cloud drive (Google Drive / OneDrive / SharePoint)", category="Documents", popular=True, free=True,
                          desc="Upload the document's PDF (and any files it generated) to Google Drive or Microsoft 365, in a "
                               "folder built from its fields — e.g. Claims/{field:Claim type}/{document_name}.",
                          fields=[F("connection", "Google Drive or Microsoft 365 connection", "connection", required=True),
                                  F("folder", "Folder", placeholder="Paperlane/{workflow_name}/{document_name}"),
                                  F("filename", "File name", placeholder="{document_name}"),
                                  F("drive", "Drive or folder id (optional)",
                                    help="A SharePoint drive id, or a Google Drive folder id to start from. Empty: your own drive."),
                                  F("generated", "Also upload files generated by bots (Word templates)", "checkbox")]),
    "docx_template": dict(name="Generate a document from a Word template", category="Documents", popular=True, free=True,
                          desc="Fill a .docx template you uploaded to this workflow with the document's data — {Field name}, "
                               "{if Amount > 10000}…{endif} — and save it (and a PDF) to the document's Files.",
                          fields=[F("template", "Template", required=True, placeholder="Offer letter.docx",
                                    help="The name of a Word file uploaded in the builder's Documents panel."),
                                  F("filename", "File name", placeholder="{document_name} – offer letter"),
                                  F("pdf", "Also save a PDF (needs LibreOffice on the server)", "checkbox"),
                                  F("include_sensitive", "Fill sensitive fields too", "checkbox")]),
    "ai_agent": dict(name="AI agent: follow instructions", category="AI", popular=True, free=True,
                     desc="Describe what should happen in plain English. The AI reads the document and does only what you "
                          "allow: fill the fields you list, add tags, write a comment, or alert the document's owner.",
                     fields=[F("instructions", "Instructions", "textarea", required=True,
                               placeholder="If the claim mentions a previous claim in the last 12 months, set Risk to High, "
                                           "tag it #repeat and alert the owner. Otherwise set Risk to Low."),
                             F("fields", "Fields it may set (optional)", placeholder="Risk, Next action",
                               help="Comma separated field names. Sensitive fields are never read or written."),
                             F("allow_tags", "It may add tags", "checkbox"),
                             F("allow_comment", "It may write a comment", "checkbox"),
                             F("allow_alert", "It may alert the document's owner", "checkbox")]),
    "ai_translate": dict(name="AI: translate fields", category="AI", free=True,
                         desc="Translate text fields into another language and write the result into other fields.",
                         fields=[F("mapping", "Source = target fields", "mapping", required=True,
                                   help="One per line: Description = Description (Spanish)"),
                                 F("language", "Language", required=True, placeholder="Spanish")]),
    "prefill_api": dict(name="Pre-fill from API", category="Webhook",
                        desc="GET JSON from an API and pre-fill fields with it.",
                        fields=[F("url", "API URL", "url", required=True, help="Use {field:Name} tokens in the URL."),
                                F("mapping", "Map JSON keys", "mapping", help="Target field = json.key.path")]),
}

for _svc in INTEGRATIONS:
    _slug = slugify(_svc).replace("-", "_")
    if _svc in CLOUD_STORAGE:
        CATALOG[f"save_{_slug}"] = dict(
            name=f"Save to {_svc}", category=_svc, integration=True, alias="cloud_save",
            desc=f"Archive completed documents to {_svc} (sent to your {_svc} connection; a local copy is kept).",
            fields=[F("connection", f"{_svc} connection", "connection"), F("folder", "Folder", placeholder="/Paperlane")])
    CATALOG[f"export_{_slug}"] = dict(
        name=f"Export to {_svc}", category=_svc, integration=True, alias="integration_push",
        desc=f"Send document data to {_svc} through your connected account endpoint.",
        fields=[F("connection", f"{_svc} connection", "connection", required=True),
                F("object", "Target object / table / channel", placeholder="e.g. Contact, Sheet1, #ops"),
                F("mapping", "Field mapping", "mapping", help="Target attribute = Field name (blank = all fields)")])
    CATALOG[f"export_{_slug}"]["provider"] = _svc
    if f"save_{_slug}" in CATALOG:
        CATALOG[f"save_{_slug}"]["provider"] = _svc

CATEGORY_TREE = [
    ("Automation", ["Documents", "Email", "Fax", "Reminder", "SMS", "Tags"]),
    ("AI", ["AI"]),
    ("Data movement", ["Fields data", "Webhook"]),
    ("Integration", INTEGRATIONS),
    ("Permissions", ["Permissions"]),
    ("Signatures", ["Signatures"]),
]


# ---------------------------------------------------------------------------------
def token_render(text, packet, context=None):
    values = packet.flat_values()
    now = timezone.localtime()
    context = context or {}
    ctx = {"document_name": packet.name, "workflow_name": packet.workflow.name, "today": now.strftime("%d/%m/%Y"),
           "now": now.strftime("%d/%m/%Y %H:%M"), "packet_id": packet.pk, "fields": values,
           "user.email": context.get("@user.email", ""), "user.name": context.get("@user.name", ""),
           "user_name": context.get("@user.name", "") or "there", "flow.name": packet.workflow.name}
    return mailer.render(text or "", ctx)


def parse_mapping(text):
    pairs = []
    for line in (text or "").splitlines():
        if "=" in line:
            left, right = line.split("=", 1)
            if left.strip():
                pairs.append((left.strip(), right.strip()))
    return pairs


def set_field(packet, field_ref, value):
    ref = str(field_ref).strip().lower()
    for doc in packet.documents:
        for f in doc.get("fields", []):
            if ref in {str(f.get("id", "")).lower(), str(f.get("name", "")).lower(), str(f.get("label", "")).lower()}:
                packet.data.setdefault(doc["key"], {})[f["id"]] = value
                return True
    return False


def _post_json(url, payload, headers=None, timeout=10):
    req = urllib.request.Request(url, data=json.dumps(payload, default=str).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    from superadmin.netguard import check_url

    check_url(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - user configured endpoint
        return resp.status


def packet_payload(packet):
    from .fieldtypes import field_value_to_text

    docs = []
    for doc in packet.documents:
        vals = packet.data.get(doc["key"], {})
        docs.append({"name": doc["name"], "fields": {
            (f.get("name") or f.get("label") or f["id"]): field_value_to_text(f, vals.get(f["id"]))
            for f in doc.get("fields", []) if f["id"] in vals}})
    return {"id": packet.pk, "name": packet.name, "workflow": packet.workflow.name, "status": packet.status,
            "version": packet.version, "created_at": packet.created_at.isoformat(),
            "completed_at": packet.completed_at.isoformat() if packet.completed_at else None,
            "tags": [t.name for t in packet.tags.all()], "documents": docs, "values": {
                k: v for k, v in packet.flat_values().items() if not str(v).startswith("data:image")}}


def _storage_write(packet, folder, filename, content):
    """Save a file through the configured storage (local disk or S3-compatible), one folder per workspace.
    Returns the path relative to that workspace's folder; a file with the same name is replaced."""
    from pathlib import PurePosixPath

    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage

    safe_folder = "/".join(slugify(p) or "folder" for p in token_render(folder or "Completed", packet).split("/") if p)
    name = (slugify(token_render(filename or "{document_name}", packet)) or f"packet-{packet.pk}") + ".pdf"
    rel = PurePosixPath(safe_folder) / name
    key = f"bot_storage/{packet.workspace.slug}/{rel}"
    if default_storage.exists(key):
        default_storage.delete(key)
    default_storage.save(key, ContentFile(content))
    return rel


def _connection(packet, cfg):
    cid = cfg.get("connection")
    if not cid:
        return None
    return Connection.objects.filter(workspace=packet.workspace, pk=cid).first()


# ---- handlers: each returns a human readable message or raises -------------------
def h_rename_packet(packet, cfg, ctx):
    new = token_render(cfg.get("pattern"), packet).strip()
    if new:
        packet.name = new[:255]
        packet.save(update_fields=["name"])
    return f"Renamed to “{packet.name}”"


def h_save_to_storage(packet, cfg, ctx):
    from .exports import packet_pdf

    rel = _storage_write(packet, cfg.get("folder") or "Completed/{workflow_name}", cfg.get("filename"), packet_pdf(packet) or b"")
    return f"Saved to storage: {rel}"


def h_cloud_save(packet, cfg, ctx):
    from .exports import packet_pdf

    provider = ctx["meta"].get("provider", "cloud")
    pdf = packet_pdf(packet) or b""
    rel = _storage_write(packet, f"{provider}/{cfg.get('folder') or ''}", None, pdf)
    conn = _connection(packet, cfg)
    if conn and conn.endpoint:
        import base64

        _post_json(conn.endpoint, {"provider": provider, "folder": cfg.get("folder"), "filename": rel.name,
                                   "content_base64": base64.b64encode(pdf).decode(), "packet": packet_payload(packet)},
                   {"Authorization": f"Bearer {conn.secret}"} if conn.secret else None)
        return f"Uploaded to {provider} via “{conn.label}” and kept local copy {rel}"
    return f"No {provider} connection endpoint configured — saved local copy {rel}"


def h_lock_packet(packet, cfg, ctx):
    packet.locked = True
    packet.save(update_fields=["locked"])
    return "Document package locked"


def h_archive_packet(packet, cfg, ctx):
    packet.archived_at = timezone.now()
    packet.save(update_fields=["archived_at"])
    return "Moved to archive"


def h_attach_certificate(packet, cfg, ctx):
    from .exports import certificate_pdf

    rel = _storage_write(packet, cfg.get("folder") or "Certificates", "certificate-{document_name}", certificate_pdf(packet) or b"")
    return f"Certificate saved: {rel}"


def _email(packet, to, subject, body, attach=False, key="custom"):
    from .exports import packet_pdf

    to = token_render(to, packet)
    atts = [(f"{packet.name}.pdf", packet_pdf(packet), "application/pdf")] if attach else None
    ok = mailer.send(packet.workspace, to, key, {"user_name": "there", "document_name": packet.name,
                                                  "workflow_name": packet.workflow.name, "fields": packet.flat_values(),
                                                  "link": _link(packet), "subject": subject, "message": token_render(body or "", packet)},
                     packet=packet, subject=token_render(subject, packet) if subject else None,
                     body=token_render(body, packet) if body else None, attachments=atts)
    if not ok:
        raise RuntimeError(f"Could not deliver email to {to or '(empty)'}")
    return f"Email sent to {to}"


def _link(packet):
    from .engine import packet_link

    return packet_link(packet)


def h_email_on_complete(packet, cfg, ctx):
    return _email(packet, cfg.get("to"), cfg.get("subject"), cfg.get("body"), bool(cfg.get("attach_pdf")), key="completed")


def h_email_on_open(packet, cfg, ctx):
    return _email(packet, cfg.get("to"), None, None, key="opened")


def h_email_custom(packet, cfg, ctx):
    return _email(packet, cfg.get("to"), cfg.get("subject"), cfg.get("body"))


def h_fax_send(packet, cfg, ctx):
    return _email(packet, cfg.get("gateway_email"), "Fax: {document_name}", "Fax transmission of {document_name}.", True)


def h_sms_send(packet, cfg, ctx):
    to = token_render(cfg.get("to"), packet)
    msg = token_render(cfg.get("message"), packet)
    conn = _connection(packet, cfg)
    if conn and conn.endpoint:
        _post_json(conn.endpoint, {"to": to, "body": msg}, {"Authorization": f"Bearer {conn.secret}"} if conn.secret else None)
        return f"SMS sent to {to} via {conn.label}"
    raise RuntimeError("No SMS gateway connection configured (Connected accounts)")


def h_reminder(packet, cfg, ctx):
    return "Reminder schedule registered (delivered by the scheduler)"


def h_add_tags(packet, cfg, ctx):
    names = [t.strip() for t in token_render(cfg.get("tags"), packet).split(",") if t.strip()]
    for n in names:
        tag, _ = Tag.objects.get_or_create(workspace=packet.workspace, name=n[:80])
        packet.tags.add(tag)
    return "Tags added: " + ", ".join(names)


def h_prefill_custom(packet, cfg, ctx):
    pairs = [(t, token_render(v, packet, ctx.get("context")).strip()) for t, v in parse_mapping(cfg.get("mapping"))]
    pairs = [(t, v) for t, v in pairs if v]
    if not pairs:
        raise RuntimeError("Nothing to pre-fill yet (values are empty)")
    done = [t for t, v in pairs if set_field(packet, t, v)]
    packet.save(update_fields=["data"])
    return f"Pre-filled {len(done)} field(s)"


def h_prefill_doc_to_doc(packet, cfg, ctx):
    values = packet.flat_values()
    done = [t for t, s in parse_mapping(cfg.get("mapping")) if s in values and set_field(packet, t, values[s])]
    packet.save(update_fields=["data"])
    return f"Copied {len(done)} field(s)"


def h_prefill_from_workflow(packet, cfg, ctx):
    qs = Packet.objects.filter(workspace=packet.workspace, workflow_id=cfg.get("workflow")).exclude(pk=packet.pk)
    qs = qs.order_by("-completed_at", "-created_at")
    own = packet.flat_values()
    source = None
    match = (cfg.get("match_field") or "").strip()
    for cand in qs[:500]:
        if not match or str(cand.flat_values().get(match, "")).strip().lower() == str(own.get(match, "")).strip().lower():
            source = cand
            break
    if not source:
        return "No matching document found in the source workflow"
    src = source.flat_values()
    pairs = parse_mapping(cfg.get("mapping")) or [(k, k) for k in src]
    done = [t for t, s in pairs if s in src and set_field(packet, t, src[s])]
    packet.save(update_fields=["data"])
    return f"Pre-filled {len(done)} field(s) from “{source.name}”"


def _prefill_from_rows(packet, rows, key_field):
    own = str(packet.flat_values().get(key_field, "")).strip().lower()
    for row in rows:
        if str(row.get(key_field, "")).strip().lower() == own:
            done = [k for k, v in row.items() if k != key_field and set_field(packet, k, v)]
            packet.save(update_fields=["data"])
            return f"Pre-filled {len(done)} field(s) from matching row"
    return f"No row matched {key_field} = {own or '(empty)'}"


def h_prefill_csv(packet, cfg, ctx):
    rows = list(csv.DictReader(io.StringIO(cfg.get("csv") or "")))
    return _prefill_from_rows(packet, rows, cfg.get("key_field", ""))


def h_prefill_datasheet(packet, cfg, ctx):
    sheet = DataSheet.objects.filter(workspace=packet.workspace, pk=cfg.get("sheet")).first()
    if not sheet:
        raise RuntimeError("Spreadsheet not found")
    rows = [r.values for r in sheet.rows.order_by("-created_at")]
    return _prefill_from_rows(packet, rows, cfg.get("key_field", ""))


def h_export_datasheet(packet, cfg, ctx):
    from .fieldtypes import field_value_to_text

    sheet, _ = DataSheet.objects.get_or_create(workspace=packet.workspace, name=(cfg.get("sheet") or "Submissions")[:160])
    wanted = [x.strip() for x in (cfg.get("fields") or "").split(",") if x.strip()]
    row = {"Document": packet.name, "Completed": timezone.localtime().strftime("%Y-%m-%d %H:%M")}
    for doc in packet.documents:
        vals = packet.data.get(doc["key"], {})
        for f in doc.get("fields", []):
            label = f.get("name") or f.get("label") or f["id"]
            if wanted and label not in wanted:
                continue
            if f["id"] in vals and f.get("type") not in ("signature", "initials"):
                row[label] = field_value_to_text(f, vals[f["id"]])
    cols = list(sheet.columns)
    for k in row:
        if k not in cols:
            cols.append(k)
    sheet.columns = cols
    sheet.save(update_fields=["columns"])
    DataSheetRow.objects.create(sheet=sheet, packet=packet, values=row)
    return f"Row added to spreadsheet “{sheet.name}”"


def h_autofill_signature(packet, cfg, ctx):
    profile = getattr(packet.created_by, "profile", None) if packet.created_by else None
    if not profile or not profile.signature:
        return "Sender has no default signature"
    set_field(packet, cfg.get("field"), profile.signature)
    packet.save(update_fields=["data"])
    return "Signature filled"


def h_webhook(packet, cfg, ctx):
    headers = {"X-Paperlane-Secret": cfg["secret"]} if cfg.get("secret") else None
    status = _post_json(cfg.get("url"), {"event": ctx["trigger"], "packet": packet_payload(packet)}, headers)
    return f"Webhook delivered (HTTP {status})"


def _dig(obj, path):
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return None
    return obj


def h_connector_action(packet, cfg, ctx):
    from . import connectors
    from .models import Connector

    if not packet.workspace.feature("connectors"):
        raise RuntimeError("Custom connectors are turned off by the platform administrator")
    conn = Connector.objects.filter(workspace=packet.workspace, pk=cfg.get("connector") or 0, active=True).first()
    if conn is None:
        raise RuntimeError("The connector is missing or turned off")
    res = connectors.run(conn, (cfg.get("action") or "").strip(), packet.flat_values())
    if not res["ok"]:
        raise RuntimeError(f"{conn.name}: {res['error'] or 'HTTP ' + str(res['status'])}")
    mapped = dict(res["mapped"])
    for field, path in parse_mapping(cfg.get("mapping")):
        v = connectors.dig(res["data"], path)
        if v is not None:
            mapped[field] = v if not isinstance(v, (dict, list)) else json.dumps(v)
    done = [k for k, v in mapped.items() if set_field(packet, k, v)]
    packet.save(update_fields=["data"])
    return f"{conn.name} · {res['method']} {res['status']} · filled {len(done)} field(s)"


def h_ai_fill_fields(packet, cfg, ctx):
    from . import ai_actions

    targets = [t for t in (cfg.get("targets") or "").split(",") if t.strip()]
    try:
        done = ai_actions.fill_fields(packet, targets, cfg.get("instructions", ""))
    except ai_actions.AIError as exc:
        raise RuntimeError(str(exc))
    return "AI filled " + (", ".join(f"{k} = {v}" for k, v in done.items()) or "nothing (no supported answer)")


def h_ai_summary(packet, cfg, ctx):
    from . import ai_actions
    from .models import Comment

    try:
        text = ai_actions.summarize(packet, cfg.get("instructions", ""))
    except ai_actions.AIError as exc:
        raise RuntimeError(str(exc))
    if cfg.get("target"):
        set_field(packet, cfg["target"], text)
        packet.save(update_fields=["data"])
    if cfg.get("add_comment") or not cfg.get("target"):
        Comment.objects.create(packet=packet, author=None, author_label="AI summary", text=text[:4000])
    return "Summary written (" + str(len(text.split())) + " words)"


def h_ai_classify(packet, cfg, ctx):
    from . import ai_actions

    try:
        done = ai_actions.classify(packet, cfg.get("target", ""), cfg.get("instructions", ""))
    except ai_actions.AIError as exc:
        raise RuntimeError(str(exc))
    return "AI chose " + (", ".join(f"{k} = {v}" for k, v in done.items()) or "nothing")


def h_ai_review(packet, cfg, ctx):
    from . import ai_actions
    from .models import Comment

    try:
        res = ai_actions.review(packet, cfg.get("rules", ""))
    except ai_actions.AIError as exc:
        raise RuntimeError(str(exc))
    if cfg.get("target"):
        set_field(packet, cfg["target"], "Passed" if res["passed"] else "Needs review")
        packet.save(update_fields=["data"])
    if not res["passed"]:
        if cfg.get("tag"):
            tag, _ = Tag.objects.get_or_create(workspace=packet.workspace, name=cfg["tag"].strip()[:80])
            packet.tags.add(tag)
        if cfg.get("add_comment"):
            Comment.objects.create(packet=packet, author=None, author_label="AI review",
                                   text="Needs review:\n" + "\n".join(f"• {i}" for i in res["issues"])[:3900])
    return "AI review: passed" if res["passed"] else f"AI review: {len(res['issues'])} issue(s) — " + "; ".join(res["issues"])[:400]


def h_save_to_drive(packet, cfg, ctx):
    from . import clouddrive
    from .exports import packet_pdf

    conn = _connection(packet, cfg)
    if conn is None or conn.kind not in ("google", "microsoft"):
        raise RuntimeError("Choose a Google Drive or Microsoft 365 connection (Account › Connections)")
    folder = token_render(cfg.get("folder") or "Paperlane/{workflow_name}", packet)
    name = (token_render(cfg.get("filename") or "{document_name}", packet).strip() or f"document-{packet.pk}")[:120]
    try:
        links = [clouddrive.upload(conn, folder, f"{name}.pdf", packet_pdf(packet) or b"", drive=(cfg.get("drive") or "").strip())]
        if cfg.get("generated"):
            for f in packet.files.filter(doc_key="generated"):
                with f.file.open("rb") as fh:
                    mime = "application/pdf" if f.original_name.lower().endswith(".pdf") else \
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    links.append(clouddrive.upload(conn, folder, f.original_name, fh.read(), mime=mime, drive=(cfg.get("drive") or "").strip()))
    except clouddrive.DriveError as exc:
        raise RuntimeError(f"{conn.label}: {exc}")
    return f"Saved {len(links)} file(s) to {conn.label} › {folder}" + (f" · {links[0]}" if links and str(links[0]).startswith("http") else "")


def h_docx_template(packet, cfg, ctx):
    from django.core.files.base import ContentFile

    from . import docgen
    from .models import PacketFile, WorkflowDocument

    want = (cfg.get("template") or "").strip().lower()
    tpl = None
    for d in packet.workflow.documents.filter(kind=WorkflowDocument.FILE):
        names = {d.name.lower(), os.path.basename(d.file.name or "").lower()}
        if d.file and (want in names or want.removesuffix(".docx") in {n.removesuffix(".docx") for n in names}):
            tpl = d
            break
    if tpl is None:
        raise RuntimeError(f"No uploaded Word template called “{cfg.get('template')}” in this workflow")
    with tpl.file.open("rb") as fh:
        data = fh.read()
    filled, missing = docgen.render(data, docgen.values_for(packet, include_sensitive=bool(cfg.get("include_sensitive"))))
    base = slugify(token_render(cfg.get("filename") or "{document_name}", packet))[:80] or f"document-{packet.pk}"
    saved = [base + ".docx"]
    PacketFile.objects.create(packet=packet, doc_key="generated", field_id=tpl.name[:128], original_name=base + ".docx",
                              file=ContentFile(filled, name=base + ".docx"))
    note = ""
    if cfg.get("pdf"):
        pdf = docgen.to_pdf(filled)
        if pdf:
            PacketFile.objects.create(packet=packet, doc_key="generated", field_id=tpl.name[:128], original_name=base + ".pdf",
                                      file=ContentFile(pdf, name=base + ".pdf"))
            saved.append(base + ".pdf")
        else:
            note = " (no PDF: LibreOffice isn't installed on the server)"
    return f"Generated {', '.join(saved)} from {tpl.name}{note}" + (f" · no field for: {', '.join(missing[:5])}" if missing else "")


def h_ai_agent(packet, cfg, ctx):
    from . import ai_actions, engine
    from .models import Comment

    try:
        res = ai_actions.agent(packet, cfg.get("instructions", ""), fields=[f for f in (cfg.get("fields") or "").split(",") if f.strip()],
                               tags=bool(cfg.get("allow_tags")), comment=bool(cfg.get("allow_comment")), alert=bool(cfg.get("allow_alert")))
    except ai_actions.AIError as exc:
        raise RuntimeError(str(exc))
    done = [k for k, v in res["set"].items() if set_field(packet, k, v)]
    if done:
        packet.save(update_fields=["data"])
    for name in res["tags"]:
        tag, _ = Tag.objects.get_or_create(workspace=packet.workspace, name=name)
        packet.tags.add(tag)
    if res["comment"]:
        Comment.objects.create(packet=packet, author=None, author_label="AI agent", text=res["comment"])
    if res["alert"] and packet.created_by:
        engine.notify(packet.created_by, packet.workspace, f"AI agent: {res['alert']}", "", f"/documents/{packet.pk}/", kind="attention")
    parts = [f"set {', '.join(f'{k} = {res['set'][k]}' for k in done)}" if done else "",
             f"tagged {', '.join('#' + t for t in res['tags'])}" if res["tags"] else "",
             "commented" if res["comment"] else "", "alerted the owner" if res["alert"] and packet.created_by else ""]
    summary = "; ".join(p for p in parts if p) or "nothing needed"
    return f"AI agent: {summary}" + (f" — {res['reason']}" if res["reason"] else "")


def h_ai_translate(packet, cfg, ctx):
    from . import ai_actions

    pairs = parse_mapping(cfg.get("mapping"))
    try:
        out = ai_actions.translate(packet, [src for src, _ in pairs], cfg.get("language", "English"))
    except ai_actions.AIError as exc:
        raise RuntimeError(str(exc))
    done = [target for src, target in pairs if src in out and set_field(packet, target, out[src])]
    packet.save(update_fields=["data"])
    return f"Translated into {cfg.get('language')}: " + (", ".join(done) or "nothing (source fields are empty)")


def _render_link(text, packet, ctx=None):
    from .engine import absolute

    return token_render((text or "").replace("{link}", absolute(f"/documents/{packet.pk}/")), packet, (ctx or {}).get("context"))


def _bump_counter(workspace, name, year=None):
    """Atomically increment a named counter stored on the workspace; returns the new value."""
    from django.db import transaction

    from accounts.models import Workspace

    key = f"{name}:{year}" if year else name
    with transaction.atomic():
        ws = Workspace.objects.select_for_update().get(pk=workspace.pk)
        counters = dict((ws.settings or {}).get("counters") or {})
        counters[key] = int(counters.get(key, 0)) + 1
        ws.settings = {**(ws.settings or {}), "counters": counters}
        ws.save(update_fields=["settings"])
        return counters[key]


def h_reference_number(packet, cfg, ctx):
    import re as _re

    flags = packet.flags or {}
    if flags.get("reference"):  # one reference per document, even if the bot runs again
        return f"Reference already set: {flags['reference']}"
    now = timezone.localtime()
    pattern = cfg.get("pattern") or "REF-{yyyy}-{seq:5}"
    n = _bump_counter(packet.workspace, (cfg.get("counter") or f"wf{packet.workflow_id}").strip()[:60],
                      now.year if cfg.get("yearly") else None)
    text = _re.sub(r"\{seq(?::(\d+))?\}", lambda m: str(n).zfill(int(m.group(1) or 0)), pattern)
    for k, v in {"{yyyy}": now.strftime("%Y"), "{yy}": now.strftime("%y"), "{mm}": now.strftime("%m"), "{dd}": now.strftime("%d")}.items():
        text = text.replace(k, v)
    ref = token_render(text, packet).strip()[:80]
    if not set_field(packet, cfg.get("target"), ref):
        raise RuntimeError(f"No field named “{cfg.get('target')}”")
    packet.flags = {**flags, "reference": ref}
    if cfg.get("rename") and not packet.name.startswith(ref):
        packet.name = f"{ref} · {packet.name}"[:255]
    packet.save(update_fields=["data", "flags", "name"])
    return f"Reference {ref}"


def h_assign_round_robin(packet, cfg, ctx):
    from accounts.models import Group

    from . import engine
    from .models import StepRun

    step_key = cfg.get("step") or ctx.get("step_key")
    runs = packet.runs.filter(status__in=[StepRun.PENDING, StepRun.OPENED], claimed_by__isnull=True)
    if step_key:
        runs = runs.filter(step_key=step_key)
    runs = list(runs.select_related("assignee_group"))
    if not runs:
        return "Nothing to assign (no open, untaken step)"
    group = Group.objects.filter(workspace=packet.workspace, pk=cfg.get("group") or 0).first() or runs[0].assignee_group
    if group is None:
        raise RuntimeError("Choose a group, or use this bot on a step assigned to a group")
    members = [m.user for m in group.members.filter(active=True).select_related("user").order_by("user_id")]
    if cfg.get("skip_away"):
        members = [u for u in members if not getattr(getattr(u, "profile", None), "out_of_office", lambda: False)()] or members
    if not members:
        raise RuntimeError(f"{group.name} has no active members")
    if cfg.get("strategy") == "rotate":
        n = _bump_counter(packet.workspace, f"rr:{group.pk}")
        user = members[(n - 1) % len(members)]
    else:
        load = {u.pk: StepRun.objects.filter(status__in=[StepRun.PENDING, StepRun.OPENED], packet__workspace=packet.workspace,
                                              packet__status="in_progress")
                .filter(models.Q(claimed_by=u) | models.Q(assignee_user=u)).count() for u in members}
        user = min(members, key=lambda u: (load[u.pk], u.pk))
    for run in runs:
        run.assignee_group = run.assignee_group or group
        run.claim(user)
        engine.notify(user, packet.workspace, f"{packet.name} was assigned to you", run.step_name, run.fill_url(), kind="assigned")
    engine.audit(packet.workspace, "step.auto_assigned", f"{runs[0].step_name} → {user.email} ({group.name})", packet=packet)
    return f"Assigned to {user.get_full_name() or user.email} from {group.name}"


def _parse_date(value):
    import datetime as _dt

    s = str(value or "").strip()[:19]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M", "%d-%m-%Y"):
        try:
            return _dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def h_transform_field(packet, cfg, ctx):
    import datetime as _dt
    import re as _re

    from . import sla

    op = cfg.get("operation") or "template"
    src = cfg.get("source") or ""
    vals = packet.flat_values()
    raw = token_render(src, packet) if ("{" in src or op == "template") else vals.get(src, next(
        (v for k, v in vals.items() if str(k).lower() == src.lower()), ""))
    raw = "" if raw is None else raw
    try:
        amount = float(cfg.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    if op in ("template", "trim"):
        out = " ".join(str(raw).split())
    elif op == "upper":
        out = str(raw).upper()
    elif op == "lower":
        out = str(raw).lower()
    elif op == "title":
        out = str(raw).title()
    elif op == "digits":
        out = _re.sub(r"\D", "", str(raw))
    elif op == "round":
        try:
            out = str(round(float(str(raw).replace(",", "")), int(amount)))
        except ValueError:
            raise RuntimeError(f"“{raw}” is not a number")
    elif op == "today":
        out = timezone.localdate().isoformat()
    elif op in ("add_days", "add_business_days"):
        d = _parse_date(raw) if raw else _dt.datetime.combine(timezone.localdate(), _dt.time())
        if d is None:
            raise RuntimeError(f"“{raw}” is not a date")
        d = sla.add_business_days(d, amount) if op == "add_business_days" else d + _dt.timedelta(days=amount)
        out = d.date().isoformat()
    else:
        raise RuntimeError(f"Unknown operation {op}")
    if not set_field(packet, cfg.get("target"), out):
        raise RuntimeError(f"No field named “{cfg.get('target')}”")
    packet.save(update_fields=["data"])
    return f"{cfg.get('target')} = {out}"


import contextvars

_CHAIN = contextvars.ContextVar("paperlane_bot_chain", default=frozenset())  # workflows being started by bots right now


def h_start_workflow(packet, cfg, ctx):
    from . import engine
    from .models import Comment, Workflow

    wf = Workflow.objects.filter(workspace=packet.workspace, pk=cfg.get("workflow") or 0, trashed_at__isnull=True).first()
    if wf is None or not wf.is_published:
        raise RuntimeError("The workflow to start is missing or not published")
    chain = _CHAIN.get()
    if wf.pk in chain or len(chain) >= 5:
        raise RuntimeError("Stopped: starting this workflow here would loop")
    prefill = {k: token_render(v, packet) for k, v in parse_mapping(cfg.get("mapping"))}
    recipient = token_render(cfg.get("recipient"), packet).strip()
    first = {"email": recipient, "name": recipient} if "@" in recipient else None
    token = _CHAIN.set(chain | {packet.workflow_id, wf.pk})
    try:
        new = engine.start_packet(wf, source="bot", prefill=prefill, first_assignee=first,
                                  name=token_render(cfg.get("name"), packet).strip()[:255] or None)
    finally:
        _CHAIN.reset(token)
    new.flags = {**(new.flags or {}), "started_by_bot": packet.pk}
    new.save(update_fields=["flags"])
    if cfg.get("link_back"):
        Comment.objects.create(packet=packet, author_label="Automation", text=f"Started #{new.pk} {new.name} ({wf.name}).")
        Comment.objects.create(packet=new, author_label="Automation", text=f"Started from #{packet.pk} {packet.name} ({packet.workflow.name}).")
    return f"Started #{new.pk} {new.name}"


def h_set_deadline(packet, cfg, ctx):
    import datetime as _dt

    from .models import StepRun

    runs = packet.runs.filter(status__in=[StepRun.PENDING, StepRun.OPENED])
    if cfg.get("step"):
        runs = runs.filter(step_key=cfg["step"])
    due = None
    if cfg.get("date_field"):
        vals = packet.flat_values()
        raw = vals.get(cfg["date_field"]) or next((v for k, v in vals.items() if str(k).lower() == cfg["date_field"].lower()), "")
        d = _parse_date(raw)
        if d is None:
            raise RuntimeError(f"“{cfg['date_field']}” has no date")
        if d.hour == 0 and d.minute == 0:
            d = d.replace(hour=17)  # a date means end of the working day
        due = timezone.make_aware(d) if timezone.is_naive(d) else d
    elif cfg.get("hours"):
        due = timezone.now() + _dt.timedelta(hours=float(cfg["hours"]))
    if due is None:
        raise RuntimeError("Choose a date field or a number of hours")
    n = runs.update(due_at=due, escalated_at=None)
    return f"Deadline {timezone.localtime(due):%b %d, %H:%M} on {n} step(s)"


def h_post_chat(packet, cfg, ctx):
    url = (cfg.get("url") or "").strip()
    if not url.startswith("https://"):
        raise RuntimeError("Use the https:// incoming-webhook URL from Teams, Slack or Google Chat")
    status = _post_json(url, {"text": _render_link(cfg.get("message"), packet, ctx)})
    return f"Posted to chat (HTTP {status})"


def h_add_comment(packet, cfg, ctx):
    from .models import Comment

    text = _render_link(cfg.get("text"), packet, ctx).strip()
    if text:
        Comment.objects.create(packet=packet, author_label=(cfg.get("author") or "Automation")[:200], text=text[:4000])
    return "Comment added"


def h_notify_team(packet, cfg, ctx):
    from django.contrib.auth import get_user_model

    from accounts.models import Group

    from . import engine

    emails = [e.strip().lower() for e in token_render(cfg.get("to"), packet).split(",") if "@" in e]
    users = list(get_user_model().objects.filter(email__in=emails, memberships__workspace=packet.workspace).distinct())
    g = Group.objects.filter(workspace=packet.workspace, pk=cfg.get("group") or 0).first()
    if g:
        users += [m.user for m in g.members.select_related("user") if m.user not in users]
    title = token_render(cfg.get("title"), packet)[:200]
    for u in users:
        engine.notify(u, packet.workspace, title, token_render(cfg.get("body"), packet), f"/documents/{packet.pk}/", kind="assigned")
    return f"Notified {len(users)} people"


def h_prefill_api(packet, cfg, ctx):
    from superadmin.netguard import check_url

    url = token_render(cfg.get("url"), packet)
    if not str(url).lower().startswith(("http://", "https://")):
        raise ValueError("Only http(s) URLs are allowed")
    check_url(url)
    with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 - user configured endpoint
        data = json.loads(resp.read().decode() or "{}")
    pairs = parse_mapping(cfg.get("mapping"))
    done = [t for t, p in pairs if _dig(data, p) is not None and set_field(packet, t, _dig(data, p))]
    packet.save(update_fields=["data"])
    return f"Pre-filled {len(done)} field(s) from API"


def h_integration_push(packet, cfg, ctx):
    provider = ctx["meta"].get("provider", "integration")
    conn = _connection(packet, cfg)
    if not conn or not conn.endpoint:
        raise RuntimeError(f"Connect a {provider} account (Connected accounts) with an endpoint URL first")
    payload = packet_payload(packet)
    pairs = parse_mapping(cfg.get("mapping"))
    if pairs:
        vals = packet.flat_values()
        payload["mapped"] = {t: vals.get(s) for t, s in pairs}
    status = _post_json(conn.endpoint, {"provider": provider, "object": cfg.get("object"), "packet": payload},
                        {"Authorization": f"Bearer {conn.secret}"} if conn.secret else None)
    return f"Sent to {provider} via “{conn.label}” (HTTP {status})"


def _assignees(packet, cfg, ctx):
    from accounts.models import Group
    from django.contrib.auth import get_user_model

    User = get_user_model()
    group = Group.objects.filter(workspace=packet.workspace, pk=cfg.get("group") or 0).first()
    if group:
        return [{"email": "", "name": group.name, "user": None, "group": group}]
    out = []
    for raw in token_render(cfg.get("recipient") or "", packet, ctx.get("context")).replace(";", ",").split(","):
        email = raw.strip()
        if "@" in email:
            out.append({"email": email, "name": email, "user": User.objects.filter(email__iexact=email).first()})
    return out or None


def h_route_to_step(packet, cfg, ctx):
    from .engine import route

    step = packet.step(cfg.get("step"))
    if not step:
        raise RuntimeError("Target step no longer exists")
    runs = route(packet, step["key"], assignees=_assignees(packet, cfg, ctx), subject=cfg.get("subject") or None,
                 body=cfg.get("body") if cfg.get("body") else None)
    return f"Sent to “{step['name']}” ({', '.join(r.assignee_email or r.assignee_name for r in runs) or 'no recipient'})"


def h_reassign_step(packet, cfg, ctx):
    """Reassign = the step's open recipients are replaced (their links stop working), then the step goes to the new ones."""
    from .models import StepRun

    step = packet.step(cfg.get("step"))
    if step and _assignees(packet, cfg, ctx):
        packet.runs.filter(step_key=step["key"], status__in=[StepRun.PENDING, StepRun.OPENED]).update(status=StepRun.REVOKED)
    return h_route_to_step(packet, cfg, ctx).replace("Sent to", "Reassigned to")


def h_calculate(packet, cfg, ctx):
    from .conditions import compute_formula

    done = []
    for target, expr in parse_mapping(cfg.get("mapping")):
        result = compute_formula(expr, packet.flat_values())
        if result != "" and set_field(packet, target, result):
            done.append(f"{target}={result}")
    packet.save(update_fields=["data"])
    return "Calculated " + (", ".join(done) or "nothing (missing inputs)")


def h_remove_tags(packet, cfg, ctx):
    names = [t.strip() for t in token_render(cfg.get("tags"), packet).split(",") if t.strip()]
    removed = list(packet.tags.filter(name__in=names).values_list("name", flat=True))
    packet.tags.remove(*packet.tags.filter(name__in=names))
    return "Tags removed: " + (", ".join(removed) or "none present")


def h_technical_revision(packet, cfg, ctx):
    from .engine import complete_run, save_revision

    save_revision(packet, note="Technical revision (bot)")
    step_key = cfg.get("step")
    if step_key:
        for run in packet.runs.filter(step_key=step_key, status__in=["pending", "opened"]):
            complete_run(run, {}, label="Technical revision bot")
            return f"Revision saved and step “{run.step_name}” completed automatically"
    return "Technical revision saved"


def h_reminder_custom(packet, cfg, ctx):
    body = cfg.get("body") or ""
    if cfg.get("with_link"):
        body += "\n\nOpen the document: {link}"
    return _email(packet, cfg.get("emails"), cfg.get("subject"), body)


HANDLERS = {k[2:]: v for k, v in globals().items() if k.startswith("h_")}


def bot_meta(bot_type):
    return CATALOG.get(bot_type, {"name": bot_type, "category": "Other", "desc": "", "fields": []})


def _condition_context(packet, context):
    from .engine import condition_context

    ctx = condition_context(packet)
    ctx.update({k: v for k, v in (context or {}).items() if v not in (None, "") or k not in ctx})
    return ctx


# bots whose effect the workflow needs at once (routing, assignment): always run in the request
FOREGROUND_ONLY = {"route_to_step", "reassign_step", "assign_round_robin", "set_deadline", "technical_revision", "rename_packet"}


def run_bot(packet, bot, trigger, context=None, *, from_job=False):
    from .models import BotJob

    meta = bot_meta(bot["bot_type"])
    handler = HANDLERS.get(meta.get("alias") or bot["bot_type"])
    log = dict(workspace=packet.workspace, workflow=packet.workflow, packet=packet, bot_key=bot["key"],
               bot_name=bot.get("name") or meta["name"], trigger=trigger)
    if handler is None:
        return BotLog.objects.create(status="failed", message="Unknown bot type", **log)
    if bot.get("run_once") and BotLog.objects.filter(packet=packet, bot_key=bot["key"], status="success").exists():
        return None
    ctx = _condition_context(packet, context)
    if not evaluate(bot.get("condition"), packet.flat_values(), ctx):
        return BotLog.objects.create(status="skipped", message="Conditions not met", **log) if not from_job else None
    from .simulator import SAFE_BOT_CATEGORIES, SIM_SKIP_TYPES, SIMULATING

    sim = SIMULATING.get()
    if bot.get("delay_minutes") and not from_job:
        BotJob.objects.create(packet=packet, bot=bot, trigger=trigger, context=ctx,
                              run_at=timezone.now() + timezone.timedelta(minutes=int(bot["delay_minutes"])))
        return BotLog.objects.create(status="success", message=f"Scheduled in {bot['delay_minutes']} min", **log)
    if (bot.get("config") or {}).get("background") and not from_job and trigger != "inline" and sim is None \
            and bot["bot_type"] not in FOREGROUND_ONLY:
        BotJob.objects.create(packet=packet, bot=bot, trigger=trigger, context=ctx, run_at=timezone.now())
        return BotLog.objects.create(status="success", message="Queued to run in the background", **log)
    if sim is not None and (meta.get("category") not in SAFE_BOT_CATEGORIES or bot["bot_type"] in SIM_SKIP_TYPES):  # never reach outside during a simulation
        sim["bots"].append({"name": bot.get("name") or meta["name"], "category": meta.get("category", ""), "trigger": trigger})
        return BotLog.objects.create(status="skipped", message="Simulation: would run", **log)
    from .history import changed_by

    try:
        with changed_by(None, f"Bot: {bot.get('name') or meta.get('name') or bot['bot_type']}", f"Bot: {bot.get('name') or bot['bot_type']}"):
            message = handler(packet, bot.get("config") or {}, {"trigger": trigger, "meta": meta, "context": ctx,
                                                                "step_key": bot.get("step_key") or (context or {}).get("@step_key")})
        status = "success"
        packet.refresh_from_db()
        for t in bot.get("success_tags") or []:
            tag, _ = Tag.objects.get_or_create(workspace=packet.workspace, name=str(t)[:80])
            packet.tags.add(tag)
    except Exception as exc:  # bots must never break the workflow
        message, status = f"{type(exc).__name__}: {exc}", "failed"
        if (bot.get("config") or {}).get("skip_on_fail"):  # skip this bot when it fails
            message, status = f"Skipped after failure — {message}", "skipped"
    UsageEvent.objects.create(workspace=packet.workspace, workflow=packet.workflow,
                              workflow_name=packet.workflow.name, kind=UsageEvent.BOT)
    return BotLog.objects.create(status=status, message=message, **log)


def run_bots(packet, trigger, step_key=None, context=None):
    """Run every bot of ``trigger``. Bots pinned to a step run only for that step; bots without a
    step but with an ``@step`` condition (diagram bots) decide for themselves."""
    for bot in packet.definition.get("bots", []):
        if bot["trigger"] != trigger:
            continue
        if trigger in ("step_start", "step_complete", "step_opened", "inline") and bot.get("step_key") and bot["step_key"] != step_key:
            continue
        if trigger == "inline" and bot.get("step_key") != step_key:
            continue
        result = run_bot(packet, bot, trigger, context)
        if result is None or getattr(result, "status", "") != "skipped":  # a skipped bot changed nothing
            packet.refresh_from_db()


# ---- time based work (reminders, expirations, schedules) ---------------------------
def step_access_expiry(step):
    exp = (step.get("settings") or {}).get("expiration") or {}
    if exp.get("enabled") and exp.get("days"):
        try:
            return timezone.now() + timezone.timedelta(days=float(exp["days"]))
        except (TypeError, ValueError):
            return None
    return None


def _reminder_policy(packet, run):
    step = packet.step(run.step_key) or {}
    rem = (step.get("settings") or {}).get("reminder") or {}
    if rem.get("enabled"):
        return float(rem.get("every_days") or 1), int(rem.get("max") or 3)
    for bot in packet.definition.get("bots", []):
        if bot["bot_type"] == "reminder" and (not bot.get("step_key") or bot["step_key"] == run.step_key):
            cfg = bot.get("config") or {}
            return float(cfg.get("every_days") or 1), int(cfg.get("max") or 3)
    return None


def tick(now=None):
    """Run reminders, access expirations and scheduled autostarts. Returns a summary dict."""
    from .engine import audit
    from .models import StepRun

    now = now or timezone.now()
    summary = {"reminders": 0, "expired": 0, "autostarted": 0}
    open_runs = StepRun.objects.filter(status__in=[StepRun.PENDING, StepRun.OPENED],
                                       packet__status=Packet.IN_PROGRESS).select_related("packet", "packet__workspace")
    for run in open_runs:
        if run.expires_at and run.expires_at <= now:
            run.status = StepRun.EXPIRED
            run.save(update_fields=["status"])
            audit(run.packet.workspace, "step.expired", f"Access expired for {run.assignee_email or run.assignee_name}", packet=run.packet)
            summary["expired"] += 1
            continue
        policy = _reminder_policy(run.packet, run)
        if not policy or not run.assignee_email:
            continue
        every, max_n = policy
        last = run.last_reminder_at or run.created_at
        if run.reminders_sent < max_n and now - last >= timezone.timedelta(days=every):
            mailer.send(run.packet.workspace, run.assignee_email, "reminder", {
                "user_name": run.assignee_name or run.assignee_email, "document_name": run.packet.name,
                "link": run.absolute_fill_url()}, packet=run.packet)
            run.reminders_sent += 1
            run.last_reminder_at = now
            run.save(update_fields=["reminders_sent", "last_reminder_at"])
            audit(run.packet.workspace, "step.reminder", f"Reminder #{run.reminders_sent} to {run.assignee_email}", packet=run.packet)
            summary["reminders"] += 1
    from .jobs import process

    jobs = process(now)
    summary["bot_jobs"] = jobs["ran"] + jobs["failed"]
    summary["bot_retries"] = jobs["retrying"]
    from .autostart import run_due

    summary["autostarted"] = run_due(now)
    from .sla import process_overdue

    summary["overdue"] = process_overdue(now)
    from .retention import run_daily

    summary["retention"] = run_daily(now)
    from .digests import run_due as send_digests

    summary["digests"] = send_digests(now)
    from .webhooks import deliver_due

    summary["webhooks"] = deliver_due(now)
    from .keydates import run_due as key_date_reminders

    summary["key_dates"] = key_date_reminders(now)
    from .semantic import refresh_all

    try:
        summary["indexed"] = refresh_all(limit=40)  # keep "Ask your documents" current
    except Exception:  # noqa: BLE001 - search indexing must never stop the scheduler
        summary["indexed"] = 0
    return summary


def catalog_for_ui():
    """Category → list of (type, meta) for the bot picker."""
    out = {}
    for key, meta in CATALOG.items():
        out.setdefault(meta["category"], []).append((key, meta))
    return out

