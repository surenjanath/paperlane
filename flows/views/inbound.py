"""Inbound email webhook (Mailgun, SendGrid, Postmark). Off unless PAPERLANE_INBOUND_SECRET is set."""
import hmac
import logging

from django.conf import settings
from django.http import Http404, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .. import inbound

log = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def inbound_email(request, secret):
    expected = settings.PAPERLANE_INBOUND_SECRET
    if not expected or not hmac.compare_digest(secret, expected):
        raise Http404
    try:
        msg = inbound.parse_request(request)
        packet = inbound.receive(msg)
    except inbound.Rejected as exc:
        log.info("inbound email rejected: %s", exc)
        return JsonResponse({"ok": False, "reason": str(exc)})  # 200 so providers don't retry a message we refused
    except ValueError:
        return JsonResponse({"ok": False, "reason": "unreadable message"}, status=400)
    return JsonResponse({"ok": True, "document": packet.pk})
