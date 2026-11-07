"""Identity check before a recipient opens their documents (step settings › Verify the recipient).

    {"method": "email"}                          a 6-digit code emailed to the recipient's address
    {"method": "sms", "phone": "{field:Phone}", "connection": <id>}   a code by SMS through an SMS gateway connection
    {"method": "code", "code": "{field:Policy number}"}               a code the sender knows (fixed or from a field)

Codes last 10 minutes; 5 wrong answers lock the link for 15 minutes; at most 5 codes are sent per hour. Passing is
remembered for the browser session, and every attempt is in the audit trail.
"""
import hashlib
import hmac
import secrets

from django.conf import settings
from django.core.cache import cache

CODE_TTL = 600
LOCK_TTL = 900
MAX_TRIES = 5
MAX_SENDS = 5


def rule(run):
    step = run.packet.step(run.step_key) or {}
    v = (step.get("settings") or {}).get("verify") or {}
    return v if v.get("method") in ("email", "sms", "code") else None


def passed(request, run):
    return bool(request.session.get(f"idv:{run.token}"))


def _hash(code, run):
    return hmac.new(settings.SECRET_KEY.encode(), f"{run.pk}:{code}".encode(), hashlib.sha256).hexdigest()


def locked(run):
    return bool(cache.get(f"idv-lock:{run.pk}"))


def destination(run, v):
    from .bots import token_render

    if v["method"] == "email":
        return run.assignee_email
    if v["method"] == "sms":
        return token_render(v.get("phone") or "", run.packet).strip()
    return ""


def masked(dest):
    if "@" in dest:
        name, _, domain = dest.partition("@")
        return f"{name[:2]}{'•' * max(1, len(name) - 2)}@{domain}"
    digits = "".join(c for c in dest if c.isdigit())
    return f"•••• {digits[-4:]}" if digits else ""


def send_code(run, v):
    """Send a new code. Returns (ok, message)."""
    from . import engine, mailer
    from .bots import _post_json
    from accounts.models import Connection

    sends_key = f"idv-sends:{run.pk}"
    if cache.get(sends_key, 0) >= MAX_SENDS:
        return False, "Too many codes were sent. Try again in an hour."
    dest = destination(run, v)
    if not dest:
        return False, "There is no address to send a code to. Contact the sender."
    code = f"{secrets.randbelow(10 ** 6):06d}"
    ws = run.packet.workspace
    if v["method"] == "email":
        subject = f"Your code for {run.packet.name}"
        body = f"Your verification code is {code}. It works for 10 minutes.\n\nIf you didn't ask for it, ignore this email."
        mailer.send(ws, dest, "custom", {"subject": subject, "message": body}, packet=run.packet, subject=subject, body=body)
    else:
        conn = Connection.objects.filter(workspace=ws, pk=v.get("connection") or 0).first() or \
            Connection.objects.filter(workspace=ws, label__icontains="sms").first()
        if not conn or not conn.endpoint:
            return False, "Text messages aren't set up for this workspace. Contact the sender."
        try:
            _post_json(conn.endpoint, {"to": dest, "body": f"{ws.name}: your code is {code}"},
                       {"Authorization": f"Bearer {conn.secret}"} if conn.secret else None)
        except Exception:  # noqa: BLE001
            return False, "The text message couldn't be sent. Try again or contact the sender."
    cache.set(f"idv-code:{run.pk}", _hash(code, run), CODE_TTL)
    cache.set(sends_key, cache.get(sends_key, 0) + 1, 3600)
    engine.audit(ws, "identity.code_sent", f"{v['method']} code to {masked(dest)}", packet=run.packet)
    return True, f"We sent a code to {masked(dest)}."


def check(request, run, v, entered):
    """True when the answer is right (and remembers it for the session)."""
    from . import engine
    from .bots import token_render

    entered = (entered or "").strip()
    if locked(run) or not entered:
        return False
    if v["method"] == "code":
        expected = token_render(v.get("code") or "", run.packet).strip()
        ok = bool(expected) and hmac.compare_digest(expected.lower(), entered.lower())
    else:
        stored = cache.get(f"idv-code:{run.pk}")
        ok = bool(stored) and hmac.compare_digest(stored, _hash(entered.replace(" ", ""), run))
    ws = run.packet.workspace
    if ok:
        request.session[f"idv:{run.token}"] = True
        cache.delete_many([f"idv-code:{run.pk}", f"idv-tries:{run.pk}"])
        engine.audit(ws, "identity.verified", f"{run.step_name}: {v['method']}", packet=run.packet, request=request)
        return True
    tries = cache.get(f"idv-tries:{run.pk}", 0) + 1
    cache.set(f"idv-tries:{run.pk}", tries, LOCK_TTL)
    if tries >= MAX_TRIES:
        cache.set(f"idv-lock:{run.pk}", True, LOCK_TTL)
    engine.audit(ws, "identity.failed", f"{run.step_name}: wrong {v['method']} code ({tries})", packet=run.packet, request=request)
    return False
