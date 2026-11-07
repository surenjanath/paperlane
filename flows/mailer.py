"""Email rendering + delivery with workspace-customisable templates."""
import html
import re
from email.utils import formataddr, parseaddr

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

DEFAULT_TEMPLATES = {
    "invite": ("Documents", "You're invited to complete the documents",
               "Hi {user_name},\n\nYou're invited to fill out {workflow_name}.\n\n{message}\n\nOpen documents: {link}\n\nBest regards,\n{workspace_name}"),
    "reminder": ("Bots", "Reminder: documents are waiting for you",
                 "Hi {user_name},\n\nThis is a friendly reminder to complete {document_name}.\n\nOpen documents: {link}\n\nBest regards,\n{workspace_name}"),
    "completed": ("Bots", "Documents have been completed",
                  "Hi {user_name},\n\n{document_name} has been completed. Now you can view and download the documents.\n\nView documents: {link}\n\nBest regards,\n{workspace_name}"),
    "step_completed": ("Documents", "A recipient completed their documents",
                       "Hi {user_name},\n\n{actor_name} completed the step \"{step_name}\" of {document_name}.\n\nView documents: {link}\n\nBest regards,\n{workspace_name}"),
    "declined": ("Documents", "Documents were declined",
                 "Hi {user_name},\n\n{actor_name} declined {document_name}.\nReason: {reason}\n\nView documents: {link}\n\nBest regards,\n{workspace_name}"),
    "opened": ("Bots", "Documents were opened",
               "Hi {user_name},\n\n{actor_name} opened {document_name}.\n\nView documents: {link}\n\nBest regards,\n{workspace_name}"),
    "autostart": ("Workflow", "A workflow was started automatically",
                  "Hi {user_name},\n\n{workflow_name} was started automatically ({reason}).\n\nView documents: {link}\n\nBest regards,\n{workspace_name}"),
    "teammate_invite": ("Access permissions", "You're invited to join {workspace_name} on Paperlane",
                        "Hi,\n\n{actor_name} invited you to join the {workspace_name} workspace as {role}.\n\nAccept the invitation: {link}\n\nBest regards,\nPaperlane"),
    "access_request": ("Access permissions", "{actor_name} requests access to {workflow_name}",
                       "Hi {user_name},\n\n{actor_name} would like to configure {workflow_name}, which you are currently editing.\n\nOpen workflow: {link}\n\nBest regards,\n{workspace_name}"),
    "custom": ("Other", "{subject}", "{message}"),
}

FOOTER = ("\n\n--\nThis email contains a secure link for document access. Please do not forward or share it.\n"
          "If you believe this email was sent by mistake, please contact the sender directly.")


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render(text, ctx):
    text = re.sub(r"\{field:([^}]+)\}", lambda m: str(ctx.get("fields", {}).get(m.group(1).strip(), "")), text or "")
    # dotted tokens such as {user.email} / {flow.name} (str.format would treat them as attribute access)
    text = re.sub(r"\{([a-z_]+\.[a-z_.]+)\}", lambda m: str(ctx.get(m.group(1), m.group(0))), text)
    text = text.replace("{", "{{").replace("}", "}}")
    text = re.sub(r"\{\{([a-z_]+)\}\}", r"{\1}", text)
    return text.format_map(_SafeDict({k: v for k, v in ctx.items() if k != "fields"}))


# placeholders that make sense per email (for the click-to-insert chips in the editor)
PLACEHOLDERS = {
    "invite": ["user_name", "workflow_name", "document_name", "step_name", "message", "link"],
    "reminder": ["user_name", "document_name", "workflow_name", "step_name", "link"],
    "completed": ["user_name", "document_name", "workflow_name", "link"],
    "step_completed": ["user_name", "actor_name", "step_name", "document_name", "link"],
    "declined": ["user_name", "actor_name", "document_name", "reason", "link"],
    "opened": ["user_name", "actor_name", "document_name", "link"],
    "autostart": ["user_name", "workflow_name", "reason", "link"],
    "teammate_invite": ["actor_name", "role", "link"],
    "access_request": ["user_name", "actor_name", "workflow_name", "link"],
    "custom": ["subject", "message"],
}
COMMON = ["workspace_name"]
DESCRIPTIONS = {
    "invite": "Sent to each recipient when a step is assigned to them.",
    "reminder": "Sent by reminder settings and reminder bots while a step is still open.",
    "completed": "Sent when every step of a document is done.",
    "step_completed": "Tells the sender/owner that someone finished their step.",
    "declined": "Tells the sender/owner that someone declined.",
    "opened": "Tells the sender/owner that a recipient opened the documents.",
    "autostart": "Sent to the workflow owner when a schedule or webhook starts a document.",
    "teammate_invite": "Invitation to join this workspace.",
    "access_request": "Asks the person editing a workflow to let someone else configure it.",
    "custom": "Used by custom-email bots, broadcasts and system messages.",
}

DEFAULT_DESIGN = {"html": True, "from_name": "", "reply_to": "", "header_color": "#1f1f1f", "header_text": "#ffffff",
                  "button_color": "#1f1f1f", "button_text": "#ffffff", "show_logo": True, "footer": "",
                  "security_note": True, "button_label": "Open documents"}


def design(workspace):
    return {**DEFAULT_DESIGN, **((workspace.settings or {}).get("email_design") or {})} if workspace else dict(DEFAULT_DESIGN)


def template_obj(workspace, key):
    """Effective template: {subject, body, enabled, cc, bcc, button_label, customized}."""
    from accounts.models import EmailTemplate

    _, subject, body = DEFAULT_TEMPLATES.get(key, DEFAULT_TEMPLATES["custom"])
    out = {"subject": subject, "body": body, "enabled": True, "cc": "", "bcc": "", "button_label": "", "customized": False}
    if workspace is not None:
        tpl = EmailTemplate.objects.filter(workspace=workspace, key=key).first()
        if tpl:
            out.update(subject=tpl.subject, body=tpl.body, enabled=tpl.enabled, cc=tpl.cc, bcc=tpl.bcc,
                       button_label=tpl.button_label, customized=True)
    return out


URL_RE = re.compile(r"(https?://[^\s<]+)")


def _btn(url, label, d):
    return (f'<table role="presentation" cellspacing="0" cellpadding="0" style="margin:18px 0"><tr><td style="border-radius:8px;'
            f'background:{d["button_color"]}"><a href="{html.escape(url)}" style="display:inline-block;padding:12px 22px;'
            f'font-weight:600;font-size:15px;color:{d["button_text"]};text-decoration:none;border-radius:8px">'
            f'{html.escape(label)}</a></td></tr></table>')


def render_html(workspace, subject, body_text, ctx, button_label=""):
    """Branded HTML version of a plain-text email: header with logo/name, the text, the {link} line as a button."""
    d = design(workspace)
    link = str(ctx.get("link") or "")
    label = button_label or d.get("button_label") or "Open"
    parts, used_button = [], False
    for block in re.split(r"\n\s*\n", body_text.strip()):
        lines = block.split("\n")
        if link and not used_button and any(link in ln for ln in lines):
            # "Open documents: https://…" → the text before the colon becomes the button label when no label is set
            ln = next(ln for ln in lines if link in ln)
            before = ln.split(link)[0].strip().rstrip(":").strip()
            rest = [x for x in lines if x is not ln]
            if rest:
                parts.append("<p>" + "<br>".join(URL_RE.sub(r'<a href="\1">\1</a>', html.escape(x)) for x in rest) + "</p>")
            parts.append(_btn(link, button_label or before or label, d))
            used_button = True
            continue
        parts.append("<p style=\"margin:0 0 14px\">" + "<br>".join(URL_RE.sub(r'<a href="\1" style="color:inherit">\1</a>', html.escape(x))
                                                       for x in lines) + "</p>")
    logo = ""
    if workspace is not None and d.get("show_logo") and getattr(workspace, "logo", None):
        try:
            from .engine import absolute

            logo = f'<img src="{html.escape(absolute(workspace.logo.url))}" alt="" height="32" style="height:32px;vertical-align:middle;margin-right:10px">'
        except Exception:
            logo = ""
    name = html.escape(workspace.name if workspace else "Paperlane")
    footer = html.escape(render(d.get("footer") or "", ctx)).replace("\n", "<br>")
    note = ("This email contains a secure link for document access. Please do not forward or share it. If you believe "
            "this email was sent by mistake, please contact the sender directly.") if d.get("security_note", True) else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{html.escape(subject)}</title></head>
<body style="margin:0;padding:0;background:#f3f3f6;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1f1f1f">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f3f3f6;padding:24px 12px"><tr><td align="center">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:600px;background:#fff;border-radius:12px;overflow:hidden;border:1px solid #e6e6ec">
<tr><td style="background:{d["header_color"]};color:{d["header_text"]};padding:16px 24px;font-weight:700;font-size:16px">{logo}{name}</td></tr>
<tr><td style="padding:26px 28px 10px;font-size:15px;line-height:1.55">{"".join(parts)}</td></tr>
{f'<tr><td style="padding:0 28px 18px;font-size:13px;color:#555">{footer}</td></tr>' if footer else ""}
</table>
{f'<p style="max-width:600px;font-size:11.5px;color:#8a8a96;margin:14px auto 0;line-height:1.5">{note}</p>' if note else ""}
</td></tr></table></body></html>"""


def get_template(workspace, key):
    t = template_obj(workspace, key)
    return t["subject"], t["body"]


def _addresses(value):
    return [e.strip() for e in re.split(r"[,;\s]+", value or "") if "@" in e]


def send(workspace, to, key, ctx, *, packet=None, subject=None, body=None, attachments=None):
    """Render template ``key`` (or explicit subject/body) and deliver it. Never raises.
    Templates switched off in My workspace › Email templates are skipped (and logged as such)."""
    from .models import OutboundEmail

    recipients = [e.strip() for e in (to if isinstance(to, (list, tuple)) else str(to).split(",")) if e and "@" in e]
    from .simulator import SIMULATING

    if SIMULATING.get() is not None:  # simulations record emails, never send them
        SIMULATING.get()["emails"].append({"to": recipients, "template": key})
        return None
    if not recipients:
        return False
    ctx = dict(ctx)
    ctx.setdefault("workspace_name", workspace.name if workspace else "Paperlane")
    t = template_obj(workspace, key)
    subject = render(subject or t["subject"], ctx)
    text = render(body or t["body"], ctx)
    d = design(workspace)
    if not t["enabled"] and body is None:
        OutboundEmail.objects.create(workspace=workspace, packet=packet, to=", ".join(recipients), subject=subject[:255],
                                     body="(not sent — this email is switched off in Email templates)\n\n" + text, kind=f"{key}:off")
        return False
    plain = text + (FOOTER if d.get("security_note", True) else "") + (("\n\n" + render(d["footer"], ctx)) if d.get("footer") else "")
    OutboundEmail.objects.create(workspace=workspace, packet=packet, to=", ".join(recipients),
                                 subject=subject[:255], body=plain, kind=key)
    try:
        from_name, from_addr = parseaddr(settings.DEFAULT_FROM_EMAIL)
        sender = formataddr((d["from_name"] or (workspace.name if workspace else from_name) or "Paperlane", from_addr))
        msg = EmailMultiAlternatives(subject, plain, sender, recipients, cc=_addresses(t["cc"]), bcc=_addresses(t["bcc"]),
                                     reply_to=_addresses(d.get("reply_to")) or None)
        if d.get("html", True):
            msg.attach_alternative(render_html(workspace, subject, text, ctx, t["button_label"]), "text/html")
        for name, content, mime in attachments or []:
            msg.attach(name, content, mime)
        msg.send(fail_silently=False)
        return True
    except Exception:  # pragma: no cover - delivery failures are logged in OutboundEmail only
        return False
