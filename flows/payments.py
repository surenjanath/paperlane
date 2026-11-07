"""Payments on start forms with Stripe Checkout (no SDK — plain HTTPS).

Configure with PAPERLANE_STRIPE_SECRET_KEY (sk_test_… / sk_live_…) and PAPERLANE_STRIPE_WEBHOOK_SECRET (whsec_…).
A paid form stores the submission as "Awaiting payment", sends the payer to Stripe, and starts the workflow when the
payment is confirmed — on the return page (by asking Stripe) or by the signed webhook, whichever comes first.
Card details never touch Paperlane."""
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from django.utils import timezone

API = "https://api.stripe.com/v1"
ZERO_DECIMAL = {"jpy", "krw", "vnd", "clp", "pyg", "xof", "xaf", "ugx", "rwf", "kmf", "gnf", "bif", "djf", "mga", "vuv", "xpf"}


class PaymentError(Exception):
    pass


def configured():
    return bool(os.environ.get("PAPERLANE_STRIPE_SECRET_KEY"))


def _stripe(method, path, data=None):
    key = os.environ.get("PAPERLANE_STRIPE_SECRET_KEY", "")
    if not key:
        raise PaymentError("Payments aren't set up on this server (PAPERLANE_STRIPE_SECRET_KEY).")
    body = urllib.parse.urlencode(data or {}).encode() if method == "POST" else None
    req = urllib.request.Request(API + path, data=body, method=method, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - fixed Stripe API host
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        try:
            msg = json.loads(exc.read()).get("error", {}).get("message", "")
        except ValueError:
            msg = ""
        raise PaymentError(f"Stripe: {msg or exc.code}")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise PaymentError(f"Couldn't reach Stripe ({exc}).")


def amount_for(form, flat):
    """(amount in minor units, currency, label) from the form's payment options, or None when nothing is due."""
    p = (form.options or {}).get("payment") or {}
    if not p.get("enabled"):
        return None
    currency = (p.get("currency") or "usd").lower()
    raw = flat.get(p["amount_field"]) if p.get("amount_field") else p.get("amount")
    try:
        major = float(re.sub(r"[^\d.]", "", str(raw or "")) or 0)
    except ValueError:
        major = 0
    if major <= 0:
        return None
    minor = int(round(major)) if currency in ZERO_DECIMAL else int(round(major * 100))
    return minor, currency, (p.get("description") or form.name)[:120]


def checkout(sub, amount, currency, label, success_url, cancel_url, email=""):
    data = {"mode": "payment", "success_url": success_url + ("&" if "?" in success_url else "?") + "session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": cancel_url, "client_reference_id": str(sub.pk),
            "line_items[0][quantity]": 1, "line_items[0][price_data][currency]": currency,
            "line_items[0][price_data][unit_amount]": amount, "line_items[0][price_data][product_data][name]": label,
            "metadata[submission]": str(sub.pk), "metadata[form]": str(sub.form_id)}
    if email and "@" in email:
        data["customer_email"] = email
    s = _stripe("POST", "/checkout/sessions", data)
    sub.payment = {"session": s["id"], "amount": amount, "currency": currency, "status": "pending", "label": label}
    sub.save(update_fields=["payment"])
    return s["url"]


def _settle(sub, session):
    """Record a paid session and start the workflow (idempotent)."""
    from . import intake
    from .engine import audit

    if session.get("payment_status") != "paid" or str(session.get("client_reference_id")) != str(sub.pk):
        return False
    if (sub.payment or {}).get("status") != "paid":
        sub.payment = {**(sub.payment or {}), "status": "paid", "paid_at": timezone.now().isoformat(),
                       "intent": session.get("payment_intent") or "", "amount_total": session.get("amount_total")}
        sub.save(update_fields=["payment"])
        intake.start_workflow(sub)
        sub.refresh_from_db()
        if sub.packet_id:
            audit(sub.form.workspace, "packet.paid", f"Paid {fmt(sub.payment)} via Stripe", packet=sub.packet)
    return True


def confirm(sub):
    """Ask Stripe whether this submission's session is paid."""
    sid = (sub.payment or {}).get("session")
    if not sid:
        return False
    return _settle(sub, _stripe("GET", f"/checkout/sessions/{sid}"))


def verify_webhook(payload, header, secret=None, tolerance=300):
    secret = secret or os.environ.get("PAPERLANE_STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        raise PaymentError("Webhook secret isn't configured.")
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    ts, sigs = parts.get("t", ""), [v for k, v in (p.split("=", 1) for p in header.split(",") if "=" in p) if k == "v1"]
    if not ts.isdigit() or abs(time.time() - int(ts)) > tolerance:
        raise PaymentError("Stale or missing timestamp.")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in sigs):
        raise PaymentError("Bad signature.")
    return json.loads(payload)


def fmt(p):
    amt = p.get("amount_total") or p.get("amount") or 0
    cur = (p.get("currency") or "").upper()
    return f"{amt if cur.lower() in ZERO_DECIMAL else amt / 100:,.2f} {cur}"
