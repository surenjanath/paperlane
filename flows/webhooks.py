"""Outgoing webhooks: signed JSON events for other systems (n8n, Zapier, Make, your own API).

Each audit event that maps to a webhook event is queued for every matching endpoint, delivered right after the
database transaction commits (in a background thread) and retried by the scheduler with backoff.

Verify a delivery:  expected = hex(HMAC_SHA256(secret, f"{X-Paperlane-Timestamp}.{raw body}"))
                    header   X-Paperlane-Signature: sha256=<expected>
"""
import hashlib
import hmac
import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import timedelta

from django.db import close_old_connections, transaction
from django.utils import timezone

EVENT_FOR_ACTION = {
    "packet.created": "document.started", "step.sent": "step.sent", "step.completed": "step.completed",
    "step.declined": "step.declined", "step.reassigned": "step.reassigned", "step.overdue": "step.overdue",
    "step.delegated": "step.delegated", "packet.completed": "document.completed", "packet.cancelled": "document.cancelled",
    "packet.form": "form.submitted", "packet.comment": "comment.added",
}
BACKOFF_MINUTES = [1, 5, 30, 120, 720]  # after attempts 1..5; the 6th failure is final
MAX_ATTEMPTS = len(BACKOFF_MINUTES) + 1
TIMEOUT = 10
THREADS = True  # tests switch this off


def sign(secret, timestamp, body):
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def _document(packet, include_values):
    from . import engine

    current = packet.current_step() or {}
    doc = {"id": packet.pk, "name": packet.name, "status": packet.status, "current_step": current.get("name"),
           "tags": sorted(packet.tags.values_list("name", flat=True)), "url": engine.absolute(f"/documents/{packet.pk}/"),
           "created_at": packet.created_at.isoformat()}
    if include_values:
        values = {}
        for k, v in packet.flat_values().items():
            if str(k).startswith("f_") or isinstance(v, (dict, list)) and not v:
                continue
            values[k] = "(signature)" if isinstance(v, str) and v.startswith("data:image") else v
        doc["values"] = values
    return doc


def queue_for_audit(event_obj):
    """Called after every audit event; cheap when the workspace has no webhooks."""
    from .models import WebhookDelivery, WebhookEndpoint

    name = EVENT_FOR_ACTION.get(event_obj.action)
    if not name:
        return []
    from accounts.models import Workspace

    ws = Workspace.objects.filter(pk=event_obj.workspace_id).only("feature_overrides").first()
    if ws is None or not ws.feature("webhooks"):
        return []
    endpoints = [e for e in WebhookEndpoint.objects.filter(workspace_id=event_obj.workspace_id, active=True)
                 if e.wants(name, event_obj.workflow_id)]
    if not endpoints:
        return []
    created = []
    base = {"event": name, "created_at": event_obj.created_at.isoformat(),
            "workspace": {"slug": event_obj.workspace.slug, "name": event_obj.workspace.name},
            "workflow": {"id": event_obj.workflow_id, "name": event_obj.workflow.name} if event_obj.workflow_id else None,
            "detail": event_obj.detail, "actor": event_obj.actor_label or None}
    for ep in endpoints:
        uid = str(uuid.uuid4())
        payload = {"id": uid, **base,
                   "document": _document(event_obj.packet, ep.include_values) if event_obj.packet_id else None}
        created.append(WebhookDelivery.objects.create(endpoint=ep, event=name, uid=uid, payload=payload))
    ids = [d.pk for d in created]
    if THREADS:
        transaction.on_commit(lambda: threading.Thread(target=_deliver_ids, args=(ids,), daemon=True).start())
    return created


def _deliver_ids(ids):
    from .models import WebhookDelivery

    close_old_connections()
    try:
        for d in WebhookDelivery.objects.filter(pk__in=ids, status=WebhookDelivery.PENDING).select_related("endpoint"):
            deliver(d)
    finally:
        close_old_connections()


def deliver(delivery, now=None):
    """POST one delivery. Returns True on a 2xx response."""
    from .models import WebhookDelivery

    ep = delivery.endpoint
    body = json.dumps(delivery.payload, default=str).encode()
    ts = str(int(time.time()))
    req = urllib.request.Request(ep.url, data=body, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": "Paperlane-Webhooks/1.0",
        "X-Paperlane-Event": delivery.event, "X-Paperlane-Delivery": delivery.uid, "X-Paperlane-Timestamp": ts,
        "X-Paperlane-Signature": f"sha256={sign(ep.secret, ts, body)}"})
    delivery.attempts += 1
    code, text, error = None, "", ""
    try:
        from superadmin.netguard import check_url

        check_url(ep.url)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            code, text = resp.status, resp.read(2000).decode(errors="replace")
    except urllib.error.HTTPError as exc:
        code, text = exc.code, exc.read(2000).decode(errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        error = str(getattr(exc, "reason", exc))[:300]
    now = now or timezone.now()
    delivery.response_code, delivery.response_body, delivery.error = code, text[:2000], error
    if code and 200 <= code < 300:
        delivery.status, delivery.delivered_at = WebhookDelivery.OK, now
    elif delivery.attempts >= MAX_ATTEMPTS:
        delivery.status = WebhookDelivery.FAILED
    else:
        delivery.next_attempt_at = now + timedelta(minutes=BACKOFF_MINUTES[delivery.attempts - 1])
        if not error and code:
            delivery.error = f"HTTP {code}"
    delivery.save()
    return delivery.status == WebhookDelivery.OK


def deliver_due(now=None, limit=50):
    from .models import WebhookDelivery

    now = now or timezone.now()
    n = 0
    for d in (WebhookDelivery.objects.filter(status=WebhookDelivery.PENDING, next_attempt_at__lte=now, endpoint__active=True)
              .select_related("endpoint").order_by("next_attempt_at")[:limit]):
        deliver(d, now)
        n += 1
    return n


def send_test(endpoint):
    """A synchronous ping so people can check their receiver from the settings page."""
    from .models import WebhookDelivery

    uid = str(uuid.uuid4())
    d = WebhookDelivery.objects.create(endpoint=endpoint, event="ping", uid=uid, payload={
        "id": uid, "event": "ping", "created_at": timezone.now().isoformat(),
        "workspace": {"slug": endpoint.workspace.slug, "name": endpoint.workspace.name},
        "detail": "Test delivery from Paperlane", "document": None, "workflow": None})
    if not deliver(d):
        d.status = WebhookDelivery.FAILED  # a test is never retried
        d.save(update_fields=["status"])
    return d
