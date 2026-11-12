"""Tamper-evident sealing: fingerprint on completion, frozen PDF, verify by code/PDF, tamper detection, re-seal."""
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings

from accounts.models import Workspace
from core.test_deep import INTEG_BLUEPRINT

User = get_user_model()


@override_settings(MEDIA_ROOT="/tmp/paperlane-test-media")
class SealingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        from flows.blueprint import build_workflow
        self.owner = User.objects.get(username="owner")
        self.ws = Workspace.objects.get(slug="demo")
        bp = {**INTEG_BLUEPRINT, "steps": [INTEG_BLUEPRINT["steps"][0]]}  # one step → completes immediately
        self.wf, _ = build_workflow(self.ws, self.owner, bp, publish=True)
        self.client.force_login(self.owner)

    def complete(self):
        from flows import engine
        from flows.mcp import Server
        p = engine.start_packet(self.wf, user=self.owner, source="api",
                                first_assignee={"email": "owner@paperlane.test", "name": "Owner", "user": self.owner})
        Server(self.ws, self.owner).t_complete_step(p.pk, values={"Policy": "POL-9", "Fund": "100"})
        p.refresh_from_db()
        return p

    def test_completion_seals_and_verifies(self):
        from flows import sealing
        p = self.complete()
        self.assertEqual(p.status, "completed")
        s = p.seals.get()
        self.assertEqual(len(s.content_hash), 64)
        self.assertRegex(s.code, r"^[0-9A-F]{4}(-[0-9A-F]{4}){3}$")
        self.assertTrue(s.pdf and s.pdf_hash)
        self.assertTrue(sealing.check(s)["intact"])
        self.assertEqual(s.signers[0]["email"], "owner@paperlane.test")
        # the download is the frozen PDF, byte for byte
        pdf = self.client.get(f"/documents/{p.pk}/download/").content
        self.assertEqual(sealing._sha(pdf), s.pdf_hash)
        # public verify: by code (any case, no dashes), by fingerprint, by PDF upload
        self.client.logout()
        for code in (s.code, s.code.lower().replace("-", ""), s.content_hash):
            self.assertContains(self.client.get(f"/verify/{code}/"), "Authentic and unchanged")
        r = self.client.post("/verify/", {"pdf": SimpleUploadedFile("x.pdf", pdf, "application/pdf")})
        self.assertContains(r, "byte-for-byte")
        r = self.client.post("/verify/", {"pdf": SimpleUploadedFile("x.pdf", pdf + b"%edit", "application/pdf")})
        self.assertContains(r, "Not recognised")
        self.assertContains(self.client.get("/verify/AAAA-BBBB-CCCC-DDDD/"), "Not recognised")

    def test_tamper_detection_and_reseal(self):
        from flows import sealing
        from flows.models import Packet
        p = self.complete()
        s = p.seals.get()
        data = dict(p.data)
        key = next(iter(data))
        data[key] = {**data[key], next(iter(data[key])): "CHANGED"}
        Packet.objects.filter(pk=p.pk).update(data=data)  # someone edits the database directly
        self.assertFalse(sealing.check(s)["intact"])
        self.assertContains(self.client.get(f"/verify/{s.code}/"), "stored record has changed")
        self.assertContains(self.client.get(f"/documents/{p.pk}/?tab=status&check=1"), "no longer matches the seal")
        emails = self.client.get(f"/documents/{p.pk}/?tab=emails")
        self.assertContains(emails, "Documents have been completed")
        self.assertNotContains(emails, "Send the document back")
        status = self.client.get(f"/documents/{p.pk}/")
        self.assertContains(status, "Send the document back")
        self.assertNotContains(status, "Documents have been completed")
        # a legitimate re-completion seals again and supersedes the old seal
        p.refresh_from_db()
        s2 = sealing.seal(p)
        s.refresh_from_db()
        self.assertEqual((s2.number, bool(s.superseded_at)), (2, True))
        self.assertContains(self.client.get(f"/verify/{s.code}/"), "newer version exists")
        self.assertEqual(sealing.seal(p).pk, s2.pk)  # unchanged content → same seal

    def test_verify_is_rate_limited(self):
        from django.core.cache import cache
        cache.clear()
        for _ in range(30):
            self.client.get("/verify/AAAA-BBBB-CCCC-DDDD/")
        self.assertEqual(self.client.get("/verify/AAAA-BBBB-CCCC-DDDD/").status_code, 429)
        cache.clear()


@override_settings(MEDIA_ROOT="/tmp/paperlane-test-media")
class DemoSealTests(TestCase):
    """The seed commands back-date finished documents; their seals must still verify, or every demo document says
    "the stored record has changed since it was sealed" on the verify page."""

    def test_every_seeded_seal_verifies(self):
        from flows import sealing
        from flows.models import DocumentSeal, Packet

        for cmd in ("seed_demo", "seed_stories", "seed_bank_demo"):
            call_command(cmd, verbosity=0)
        done = Packet.objects.filter(status=Packet.COMPLETED)
        self.assertGreater(done.count(), 5)
        for p in done:
            seals = list(p.seals.filter(superseded_at__isnull=True))
            self.assertEqual(len(seals), 1, p.name)
            self.assertTrue(sealing.check(seals[0])["intact"], p.name)
            self.assertEqual(seals[0].sealed_at, p.completed_at)
        self.assertEqual(DocumentSeal.objects.filter(superseded_at__isnull=False).count(), 0)
        r = self.client.get(f"/verify/{DocumentSeal.objects.first().code}/")
        self.assertNotContains(r, "has changed since it was sealed")
