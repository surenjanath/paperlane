"""Start workflows from email: forward a message to the workflow's address and the AI pre-fills the first step.

Mail arrives either through a provider's inbound webhook (Mailgun, SendGrid, Postmark: ``/inbound/email/<secret>/``) or
from your own mail server piping the raw message to ``manage.py inbound_email``. Each workflow's ``email`` autostart has
a token; the address is ``<token>@PAPERLANE_INBOUND_DOMAIN`` (``anything+<token>@…`` also works).

Safety: only workspace members may start by default (or senders from allowed domains); repeated Message-IDs are
ignored; each workflow takes at most ``HOURLY_CAP`` emails an hour; sensitive fields are never sent to the AI."""
import base64
import email
import email.policy
import html
import logging
import re
from email.utils import getaddresses, parseaddr

from django.conf import settings
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.utils import timezone

log = logging.getLogger(__name__)
TOKEN = re.compile(r"(?:^|\+)([a-f0-9]{12})@", re.I)
HOURLY_CAP = 30
MAX_FILES, MAX_FILE_BYTES = 10, 15 * 1024 * 1024
SKIP = {"signature", "initials", "attachment", "heading", "description", "divider", "page_break", "html", "image", "formula",
        "youtube", "vimeo", "clickwrap"}


class Rejected(Exception):
    """The message was not accepted (the reason is safe to log)."""


def domain():
    return getattr(settings, "PAPERLANE_INBOUND_DOMAIN", "") or ""


def address(item):
    return f"{item['token']}@{domain()}" if domain() and item.get("token") else ""


# ---- parsing ----------------------------------------------------------------------------------------------------
def _strip_html(text):
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


def _addresses(values):
    """getaddresses() over non-empty header values (Python 3.13+ rejects the whole list if one is empty)."""
    return [a.lower() for _, a in getaddresses([v for v in values if v and str(v).strip()]) if a]


def parse_mime(raw):
    msg = email.message_from_bytes(raw if isinstance(raw, bytes) else raw.encode(), policy=email.policy.default)
    body = msg.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body else ""
    if body is not None and body.get_content_type() == "text/html":
        text = _strip_html(text)
    files = []
    for part in msg.iter_attachments():
        data = part.get_payload(decode=True) or b""
        files.append((part.get_filename() or "attachment", data, part.get_content_type()))
    name, addr = parseaddr(str(msg.get("From", "")))
    to = _addresses([str(msg.get(h, "")) for h in ("To", "Cc", "Delivered-To", "X-Original-To")])
    return {"to": to, "from": addr.lower(), "from_name": name, "subject": str(msg.get("Subject", "")), "text": text,
            "message_id": str(msg.get("Message-ID", "")).strip(), "attachments": files}


def parse_request(request):
    """Normalise Mailgun / SendGrid (multipart) and Postmark (JSON) inbound posts."""
    import json

    if request.content_type == "application/json":
        d = json.loads(request.body or b"{}")
        to = [x.get("Email", "") for x in d.get("ToFull") or []] or [d.get("OriginalRecipient") or d.get("To") or ""]
        files = [(a.get("Name", "attachment"), base64.b64decode(a.get("Content") or ""), a.get("ContentType", ""))
                 for a in d.get("Attachments") or []]
        frm = (d.get("FromFull") or {}).get("Email") or parseaddr(d.get("From", ""))[1]
        return {"to": to, "from": frm.lower(), "from_name": (d.get("FromFull") or {}).get("Name", ""), "subject": d.get("Subject", ""),
                "text": d.get("TextBody") or _strip_html(d.get("HtmlBody", "")), "message_id": d.get("MessageID", ""), "attachments": files}
    p = request.POST
    if p.get("email"):  # SendGrid "raw" mode posts the whole MIME message
        return parse_mime(p["email"].encode())
    to = _addresses([p.get("recipient", ""), p.get("to", ""), p.get("To", "")])
    name, frm = parseaddr(p.get("from") or p.get("sender") or p.get("From", ""))
    files = [(f.name, f.read(), f.content_type) for f in list(request.FILES.values())[:MAX_FILES]]
    return {"to": to, "from": frm.lower(), "from_name": name, "subject": p.get("subject") or p.get("Subject", ""),
            "text": p.get("stripped-text") or p.get("body-plain") or p.get("text") or _strip_html(p.get("body-html") or p.get("html", "")),
            "message_id": p.get("Message-Id") or p.get("message-id", ""), "attachments": files}


# ---- routing ----------------------------------------------------------------------------------------------------
def find(to_addresses):
    """(workflow, item) for the first recipient carrying a known token."""
    from .models import Workflow

    for a in to_addresses:
        m = TOKEN.search(a or "")
        if not m:
            continue
        token = m.group(1).lower()
        from django.db.models import TextField
        from django.db.models.functions import Cast

        candidates = Workflow.objects.annotate(_auto=Cast("autostart", TextField())).filter(_auto__contains=token, trashed_at__isnull=True)
        for wf in candidates:
            item = next((i for i in (wf.autostart or {}).get("items", []) if i.get("type") == "email" and i.get("token") == token), None)
            if item:
                return wf, item
    return None, None


def _sender(wf, item, addr):
    """The sender's user when they are a member; raises Rejected when they may not start this workflow."""
    from accounts.models import Membership

    m = Membership.objects.filter(workspace=wf.workspace, user__email__iexact=addr, user__is_active=True).select_related("user").first() \
        if addr else None
    if m:
        return m.user
    who = item.get("senders", "members")
    dom = addr.rsplit("@", 1)[-1] if "@" in addr else ""
    allowed = {d.strip().lower().lstrip("@") for d in str(item.get("domains", "")).replace(";", ",").split(",") if d.strip()}
    if who == "domains" and dom in allowed:
        return None
    raise Rejected(f"{addr or 'unknown sender'} may not start “{wf.name}” by email")


def first_step_fields(snapshot):
    """{doc_key: [field, …]} the first person fills: their own fields plus fields anyone may fill."""
    from .fieldtypes import field_roles, is_input

    first = next((s for s in snapshot.get("steps", []) if s.get("kind") not in ("branch", "bot")), None)
    out = {}
    for d in snapshot.get("documents", []):
        fs = [f for f in d.get("fields", []) if is_input(f.get("type")) and f.get("type") not in SKIP and not f.get("sensitive")
              and not f.get("readonly") and not f.get("hidden")
              and ("anyone" in field_roles(f) or (first and first["key"] in field_roles(f)))]
        if fs:
            out[d["key"]] = fs
    return out


def receive(msg):
    """Start the workflow for one normalised message. Returns the packet; raises Rejected when not accepted."""
    from . import ai_actions, engine
    from .models import Comment, Packet, PacketFile

    wf, item = find(msg.get("to") or [])
    if wf is None:
        raise Rejected("no workflow uses this address")
    if not item.get("enabled", True) or not wf.is_published:
        raise Rejected(f"email start is off for “{wf.name}”")
    user = _sender(wf, item, msg.get("from", ""))
    mid = (msg.get("message_id") or "")[:250]
    if mid and Packet.objects.filter(workflow=wf, flags__email__message_id=mid).exists():
        raise Rejected("this message already started a document")
    key = f"inbound:{wf.pk}:{timezone.now():%Y%m%d%H}"
    if cache.get(key, 0) >= HOURLY_CAP:
        raise Rejected(f"more than {HOURLY_CAP} emails this hour for “{wf.name}”")
    cache.set(key, cache.get(key, 0) + 1, 3700)

    text = f"Subject: {msg.get('subject', '')}\nFrom: {msg.get('from_name', '')} <{msg.get('from', '')}>\n\n{msg.get('text', '')}"
    prefill, ai_note = {}, ""
    if item.get("ai_fill", True):
        fields = first_step_fields(wf.runnable_snapshot())
        try:
            values = ai_actions.extract_fields(wf.workspace, fields, text, user=user) if fields else {}
            prefill = {fid: v for vals in values.values() for fid, v in vals.items()}
            ai_note = f"AI filled {len(prefill)} field{'s' if len(prefill) != 1 else ''} from the email." if prefill else "AI found nothing to fill."
        except Exception as exc:  # noqa: BLE001 - the document still starts; people fill it in
            ai_note = f"AI could not read the email ({type(exc).__name__}); fill the form in by hand."
            log.warning("inbound AI fill failed for %s: %s", wf.pk, exc)
    from .history import changed_by

    who = {"email": msg.get("from", ""), "name": msg.get("from_name") or msg.get("from", ""), "user": user}
    with changed_by(user, f"AI from email ({msg.get('from', '')})", "Email"):
        packet = engine.start_packet(wf, user=user, source="email", name=(msg.get("subject") or "").strip()[:200] or None,
                                     prefill=prefill or None, first_assignee=who)
    packet.flags = {**(packet.flags or {}), "email": {"message_id": mid, "from": msg.get("from", ""), "subject": msg.get("subject", "")[:200]}}
    packet.save(update_fields=["flags"])
    files = 0
    for name, data, _ctype in (msg.get("attachments") or [])[:MAX_FILES]:
        if not data or len(data) > MAX_FILE_BYTES:
            continue
        pf = PacketFile(packet=packet, doc_key=(packet.documents[0]["key"] if packet.documents else ""), field_id="email", original_name=name[:255])
        pf.file.save(name[:120] or "attachment", ContentFile(data), save=True)
        files += 1
    body = (msg.get("text") or "").strip()
    Comment.objects.create(packet=packet, author=user, author_label=f"Email from {msg.get('from', '')}",
                           text=(f"{msg.get('subject', '')}\n\n{body}"[:4000] + (f"\n\n{files} attachment{'s' if files != 1 else ''} saved." if files else "")))
    engine.audit(wf.workspace, "packet.from_email", f"Started from an email by {msg.get('from', '')}. {ai_note}".strip(), actor=user, packet=packet)
    item["count"] = int(item.get("count") or 0) + 1
    item["last_run"] = timezone.now().isoformat()
    from . import autostart as auto_mod

    auto_mod.save_items(wf, [item if i.get("id") == item.get("id") else i for i in auto_mod.items(wf)])
    run = engine.open_runs(packet).filter(assignee_email__iexact=msg.get("from", "")).first()
    if run is not None:  # the sender fills the first step: send them the link to review what the AI filled
        from . import mailer

        mailer.send(wf.workspace, msg["from"], "invite", {
            "user_name": msg.get("from_name") or msg["from"], "workflow_name": wf.name, "document_name": packet.name,
            "link": run.absolute_fill_url(), "fields": packet.flat_values(), "sender_name": "Paperlane",
            "message": f"Your email started “{wf.name}”. {ai_note} Review it and submit."}, packet=packet)
    return packet
