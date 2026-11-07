"""My workspace › Webhooks: outgoing, signed event deliveries for other systems."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.validators import URLValidator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .. import webhooks
from ..models import WebhookDelivery, WebhookEndpoint, Workflow

MAX_ENDPOINTS = 20


def _manager(request):
    if not request.membership or not request.membership.can_manage:
        raise PermissionDenied("Only workspace owners and supervisors can manage webhooks.")


def _apply(request, ep):
    url = request.POST.get("url", "").strip()
    URLValidator(schemes=["http", "https"])(url)
    ep.url = url
    ep.description = request.POST.get("description", "").strip()[:120]
    allowed = dict(WebhookEndpoint.EVENTS)
    ep.events = [e for e in request.POST.getlist("events") if e in allowed]
    ep.workflow = Workflow.objects.filter(workspace=request.workspace, pk=request.POST.get("workflow") or 0).first()
    ep.include_values = request.POST.get("include_values") == "on"
    ep.active = request.POST.get("active", "on") == "on"


@login_required
def webhook_list(request):
    from superadmin.features import feature_off

    if (off := feature_off(request, "webhooks")) is not None:
        return off
    _manager(request)
    ws = request.workspace
    if request.method == "POST":
        if WebhookEndpoint.objects.filter(workspace=ws).count() >= MAX_ENDPOINTS:
            messages.error(request, f"A workspace can have up to {MAX_ENDPOINTS} webhooks.")
            return redirect("flows:webhooks")
        ep = WebhookEndpoint(workspace=ws, created_by=request.user)
        try:
            _apply(request, ep)
        except Exception:
            messages.error(request, "Enter a valid http(s) URL.")
            return redirect("flows:webhooks")
        ep.save()
        messages.success(request, "Webhook added. Use “Send test” to check your receiver.")
        return redirect("flows:webhook_detail", ep.pk)
    endpoints = list(WebhookEndpoint.objects.filter(workspace=ws).select_related("workflow"))
    for ep in endpoints:
        last = ep.deliveries.first()
        ep.last = last
        ep.failed_24h = ep.deliveries.filter(status=WebhookDelivery.FAILED).count()
    from django.core.cache import cache

    return render(request, "flows/webhooks.html", {"section": "webhooks", "endpoints": endpoints, "events": WebhookEndpoint.EVENTS,
                                                   "heartbeat": cache.get("scheduler-heartbeat"),
                                                   "workflows": Workflow.objects.filter(workspace=ws).live().exclude(kind="document")})


@login_required
def webhook_detail(request, pk):
    from superadmin.features import feature_off

    if (off := feature_off(request, "webhooks")) is not None:
        return off
    _manager(request)
    ep = get_object_or_404(WebhookEndpoint, workspace=request.workspace, pk=pk)
    if request.method == "POST":
        try:
            _apply(request, ep)
        except Exception:
            messages.error(request, "Enter a valid http(s) URL.")
            return redirect("flows:webhook_detail", ep.pk)
        ep.save()
        messages.success(request, "Webhook saved.")
        return redirect("flows:webhook_detail", ep.pk)
    status = request.GET.get("status", "")
    deliveries = ep.deliveries.all()
    if status in ("ok", "failed", "pending"):
        deliveries = deliveries.filter(status=status)
    return render(request, "flows/webhook_detail.html", {
        "section": "webhooks", "ep": ep, "events": WebhookEndpoint.EVENTS, "deliveries": deliveries[:100], "status": status,
        "workflows": Workflow.objects.filter(workspace=request.workspace).live().exclude(kind="document"),
        "show": request.GET.get("delivery"),
    })


@login_required
@require_POST
def webhook_action(request, pk, action):
    from superadmin.features import feature_off

    if (off := feature_off(request, "webhooks")) is not None:
        return off
    _manager(request)
    ep = get_object_or_404(WebhookEndpoint, workspace=request.workspace, pk=pk)
    back = redirect("flows:webhook_detail", ep.pk)
    if action == "test":
        d = webhooks.send_test(ep)
        if d.status == WebhookDelivery.OK:
            messages.success(request, f"Test delivered — your endpoint answered {d.response_code}.")
        else:
            messages.error(request, f"Test failed: {d.error or f'HTTP {d.response_code}'}.")
    elif action == "redeliver":
        old = get_object_or_404(WebhookDelivery, endpoint=ep, pk=request.POST.get("delivery"))
        import uuid

        new = WebhookDelivery.objects.create(endpoint=ep, event=old.event, uid=str(uuid.uuid4()),
                                             payload={**old.payload, "id": None, "redelivery_of": old.uid})
        new.payload["id"] = new.uid
        new.save(update_fields=["payload"])
        ok = webhooks.deliver(new)
        messages.success(request, "Redelivered.") if ok else messages.error(request, f"Redelivery failed: {new.error or new.response_code}. It will be retried.")
    elif action == "rotate":
        import secrets

        ep.secret = secrets.token_hex(32)
        ep.save(update_fields=["secret"])
        messages.success(request, "New signing secret created — update your receiver.")
    elif action == "toggle":
        ep.active = not ep.active
        ep.save(update_fields=["active"])
        messages.success(request, "Webhook paused." if not ep.active else "Webhook resumed.")
    elif action == "delete":
        ep.delete()
        messages.success(request, "Webhook deleted.")
        return redirect("flows:webhooks")
    return back
