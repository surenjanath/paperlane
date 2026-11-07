"""Stripe payments on start forms: checkout redirect, workflow only after payment, return-page confirm, signed webhook."""
import hashlib
import hmac
import json
import time
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from accounts.models import Workspace
from flows.models import FormSubmission, IntakeForm, Packet

User = get_user_model()
ENV = {"PAPERLANE_STRIPE_SECRET_KEY": "sk_test_x", "PAPERLANE_STRIPE_WEBHOOK_SECRET": "whsec_test"}


class PaymentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        from flows.blueprint import build_form, build_workflow
        from core.test_deep import INTEG_BLUEPRINT
        self.owner = User.objects.get(username="owner")
        self.ws = Workspace.objects.get(slug="demo")
        self.wf, _ = build_workflow(self.ws, self.owner, INTEG_BLUEPRINT, publish=True)
        self.form = build_form(self.ws, self.owner, {"name": "Paid intake", "workflow_id": self.wf.pk, "publish": True, "fields": [
            {"id": "pol", "type": "text", "label": "Policy", "required": True}]})
        if isinstance(self.form, tuple):
            self.form = self.form[0]
        self.form = IntakeForm.objects.get(pk=getattr(self.form, "pk", None) or IntakeForm.objects.latest("pk").pk)
        self.form.options = {**(self.form.options or {}), "payment": {"enabled": True, "amount": "25.00", "currency": "ttd", "description": "Fee"}}
        self.form.save()
        self.url = self.form.public_url()

    def stripe(self, paid=True):
        calls = []

        def fake(method, path, data=None):
            calls.append((method, path, data))
            if path == "/checkout/sessions":
                return {"id": "cs_123", "url": "https://checkout.stripe.test/cs_123"}
            return {"id": "cs_123", "payment_status": "paid" if paid else "unpaid", "client_reference_id": str(self.sub().pk),
                    "amount_total": 2500, "payment_intent": "pi_1"}
        return mock.patch("flows.payments._stripe", side_effect=fake), calls

    def sub(self):
        return FormSubmission.objects.filter(form=self.form).latest("pk")

    def submit(self):
        return self.client.post(self.url, {f"f_{self.form.as_doc()['key']}_pol": "POL-1"})

    def test_checkout_then_confirm_on_return(self):
        patch, calls = self.stripe()
        with mock.patch.dict("os.environ", ENV), patch:
            r = self.submit()
            self.assertEqual(r["Location"], "https://checkout.stripe.test/cs_123")
            s = self.sub()
            self.assertEqual((s.status, s.packet_id), ("unpaid", None))  # no workflow before payment
            self.assertEqual(calls[0][2]["line_items[0][price_data][unit_amount]"], 2500)
            self.assertEqual(calls[0][2]["line_items[0][price_data][currency]"], "ttd")
            r = self.client.get(self.form_done() + "?session_id=cs_123")
        s.refresh_from_db()
        self.assertEqual(s.status, "started")
        self.assertEqual(s.payment["status"], "paid")
        self.assertContains(r, "Payment received")
        self.assertTrue(Packet.objects.filter(pk=s.packet_id, audit_events__action="packet.paid").exists())

    def form_done(self):
        from django.urls import reverse
        return reverse("flows:form_done", args=[self.form.token])

    def test_unpaid_return_waits_and_webhook_settles(self):
        patch, _ = self.stripe(paid=False)
        with mock.patch.dict("os.environ", ENV), patch:
            self.submit()
            r = self.client.get(self.form_done() + "?session_id=cs_123")
        self.assertContains(r, "Waiting for your payment")
        s = self.sub()
        self.assertIsNone(s.packet_id)
        event = {"type": "checkout.session.completed", "data": {"object": {"id": "cs_123", "payment_status": "paid",
                                                                           "client_reference_id": str(s.pk), "amount_total": 2500}}}
        body = json.dumps(event).encode()
        ts = str(int(time.time()))
        sig = hmac.new(b"whsec_test", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        with mock.patch.dict("os.environ", ENV):
            bad = self.client.post("/payments/stripe/webhook/", body, content_type="application/json", HTTP_STRIPE_SIGNATURE=f"t={ts},v1=deadbeef")
            ok = self.client.post("/payments/stripe/webhook/", body, content_type="application/json", HTTP_STRIPE_SIGNATURE=f"t={ts},v1={sig}")
            again = self.client.post("/payments/stripe/webhook/", body, content_type="application/json", HTTP_STRIPE_SIGNATURE=f"t={ts},v1={sig}")
        self.assertEqual((bad.status_code, ok.status_code, again.status_code), (400, 200, 200))
        s.refresh_from_db()
        self.assertEqual(s.status, "started")
        self.assertEqual(Packet.objects.filter(form_submissions=s).count(), 1)  # idempotent

    def test_not_configured_keeps_answers(self):
        with mock.patch.dict("os.environ", {"PAPERLANE_STRIPE_SECRET_KEY": ""}):
            r = self.submit()
        self.assertEqual(r.status_code, 502)
        self.assertContains(r, "Your answers are saved", status_code=502)
        self.assertEqual(self.sub().status, "unpaid")

    def test_free_forms_unchanged(self):
        self.form.options["payment"]["enabled"] = False
        self.form.save()
        self.submit()
        self.assertEqual(self.sub().status, "started")
