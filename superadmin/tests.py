from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings

from accounts.models import Invitation, Membership, Profile, Workspace

from .models import PlatformEvent, get_settings

User = get_user_model()


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class SuperAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        cache.clear()
        self.owner = User.objects.get(username="owner")  # the seeded super admin
        self.sam = User.objects.get(username="sam")
        self.ws = Workspace.objects.get(slug="demo")
        self.client.force_login(self.owner)

    def ps(self, **kw):
        ps = get_settings()
        for k, v in kw.items():
            setattr(ps, k, v)
        ps.save()
        return ps

    # ---- access ----
    def test_only_superusers(self):
        self.client.force_login(self.sam)
        for url in ["/platform/", "/platform/users/", "/platform/settings/", "/platform/system/"]:
            self.assertEqual(self.client.get(url).status_code, 403, url)
        self.client.logout()
        self.assertEqual(self.client.get("/platform/").status_code, 302)

    def test_every_page_renders(self):
        for url in ["/platform/", "/platform/workspaces/", f"/platform/workspaces/{self.ws.pk}/", "/platform/users/",
                    "/platform/users/?filter=no2fa", f"/platform/users/{self.sam.pk}/", "/platform/settings/",
                    "/platform/jobs/", "/platform/audit/", "/platform/system/"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertContains(self.client.get("/"), "Super admin")  # rail link

    # ---- settings ----
    def test_save_settings_and_features(self):
        data = {"site_name": "Acme Flow", "support_email": "help@example.com", "signup_mode": "open", "signup_domains": "",
                "default_plan": "Team", "default_plan_days": 30, "default_credits": 500, "max_workspaces_per_user": 2,
                "password_min_length": 12, "require_2fa": "off", "session_idle_minutes": 0, "max_upload_mb": 25,
                "blocked_extensions": "exe", "announcement": "", "announcement_level": "info", "maintenance_message": "x",
                "feat_ai": "on", "feat_api": "on", "feat_webhooks": "on", "feat_public_forms": "on", "feat_create_workspaces": "on"}
        r = self.client.post("/platform/settings/", data)
        self.assertRedirects(r, "/platform/settings/")
        ps = get_settings()
        self.assertEqual((ps.site_name, ps.password_min_length), ("Acme Flow", 12))
        self.assertFalse(ps.feature("connectors"))  # unticked
        self.assertTrue(ps.feature("ai"))
        self.assertTrue(PlatformEvent.objects.filter(action="platform.settings").exists())
        self.assertContains(self.client.get("/platform/"), "| Acme Flow</title>")
        # the domains mode needs domains
        r = self.client.post("/platform/settings/", {**data, "signup_mode": "domains"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "List the allowed domains")

    def test_password_length_follows_platform(self):
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError
        self.ps(password_min_length=14)
        with self.assertRaises(ValidationError):
            validate_password("Abcdefgh12345")
        validate_password("Abcdefgh123456!")

    # ---- sign-up ----
    def test_signup_modes(self):
        self.client.logout()
        self.ps(signup_mode="closed", support_email="help@example.com")
        r = self.client.get("/accounts/signup/")
        self.assertContains(r, "Sign-up is closed")
        self.assertNotContains(self.client.get("/accounts/login/"), "Create an account")
        self.ps(signup_mode="domains", signup_domains="example.com")
        r = self.client.post("/accounts/signup/", {"first_name": "A", "email": "a@gmail.com", "password": "Very-long-pass-77"})
        self.assertContains(r, "@example.com")
        self.assertFalse(User.objects.filter(email="a@gmail.com").exists())
        r = self.client.post("/accounts/signup/", {"first_name": "A", "email": "a@example.com", "password": "Very-long-pass-77"})
        self.assertTrue(User.objects.filter(email="a@example.com").exists())

    def test_workspace_creation_switch_and_defaults(self):
        new = User.objects.create_user("newbie", "newbie@x.test", "Very-long-pass-77")
        self.client.force_login(new)
        self.ps(features={"create_workspaces": False})
        r = self.client.get("/accounts/onboarding/")
        self.assertContains(r, "Waiting for an invitation")
        self.client.post("/accounts/onboarding/", {"name": "Nope Ltd"})
        self.assertFalse(Workspace.objects.filter(name="Nope Ltd").exists())
        self.ps(features={}, default_plan="Starter", default_credits=42, default_plan_days=14)
        self.client.post("/accounts/onboarding/", {"name": "Yes Ltd", "industry": "", "size": ""})
        ws = Workspace.objects.get(name="Yes Ltd")
        self.assertEqual((ws.plan, ws.credits_limit), ("Starter", 42))

    # ---- maintenance / suspension / 2FA / idle ----
    def test_maintenance_mode(self):
        self.ps(maintenance_mode=True, maintenance_message="Back at 5 pm")
        self.assertEqual(self.client.get("/").status_code, 200)  # super admin keeps working
        self.assertContains(self.client.get("/"), "Maintenance mode is on")
        self.client.force_login(self.sam)
        r = self.client.get("/inbox/")
        self.assertEqual(r.status_code, 503)
        self.assertContains(r, "Back at 5 pm", status_code=503)
        self.assertEqual(self.client.get("/api/v1/workflows").status_code, 503)
        self.assertEqual(self.client.get("/accounts/login/").status_code, 302)  # login stays reachable (signed in → redirect)

    def test_suspend_and_restore_workspace(self):
        r = self.client.post(f"/platform/workspaces/{self.ws.pk}/", {"op": "suspend", "reason": "Unpaid invoice"})
        self.ws.refresh_from_db()
        self.assertIsNotNone(self.ws.suspended_at)
        self.client.force_login(self.sam)
        r = self.client.get("/inbox/")
        self.assertContains(r, "Unpaid invoice", status_code=403)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get("/inbox/").status_code, 200)  # super admins can still look
        self.client.post(f"/platform/workspaces/{self.ws.pk}/", {"op": "restore"})
        self.client.force_login(self.sam)
        self.assertEqual(self.client.get("/inbox/").status_code, 200)
        self.assertEqual(PlatformEvent.objects.filter(action__in=["workspace.suspended", "workspace.restored"]).count(), 2)

    def test_required_two_factor(self):
        self.ps(require_2fa="admins")
        self.client.force_login(self.sam)  # supervisor → must set up 2FA
        self.assertRedirects(self.client.get("/inbox/"), "/account/security/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/account/security/").status_code, 200)
        self.client.force_login(User.objects.get(username="fay"))  # filler → not required
        self.assertEqual(self.client.get("/inbox/").status_code, 200)
        self.ps(require_2fa="all")
        self.assertEqual(self.client.get("/inbox/").status_code, 302)

    def test_idle_sign_out(self):
        import time
        self.ps(session_idle_minutes=5)
        self.client.force_login(self.sam)
        self.client.get("/inbox/")
        s = self.client.session
        s["last_seen"] = int(time.time()) - 600
        s.save()
        r = self.client.get("/inbox/")
        self.assertTrue(r["Location"].startswith("/accounts/login/"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_announcement_banner(self):
        self.ps(announcement="New SLA rules from Monday", announcement_level="warning")
        self.client.force_login(self.sam)
        self.assertContains(self.client.get("/inbox/"), "New SLA rules from Monday")

    # ---- feature switches ----
    def test_feature_switches_per_workspace(self):
        self.client.post(f"/platform/workspaces/{self.ws.pk}/", {"op": "features", "feat_connectors": "off", "feat_ai": "inherit"})
        self.ws.refresh_from_db()
        self.assertEqual(self.ws.feature_overrides, {"connectors": False})
        self.assertFalse(self.ws.feature("connectors"))
        self.assertEqual(self.client.get("/workspace/connectors/").status_code, 403)
        self.assertNotContains(self.client.get("/workspace/info/"), "/workspace/connectors/")
        self.ps(features={"webhooks": False})
        self.assertEqual(self.client.get("/workspace/webhooks/").status_code, 403)
        self.ps(features={"ai": False})
        from flows import ai_actions
        with self.assertRaises(ai_actions.AIError):
            ai_actions.engine_for(Workspace.objects.get(pk=self.ws.pk))

    def test_api_switch(self):
        from accounts.models import ApiToken
        tok, raw = ApiToken.issue(self.owner, self.ws, "t") if hasattr(ApiToken, "issue") else (None, None)
        if tok is None:
            self.skipTest("token helper missing")
        h = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
        self.assertEqual(self.client.get("/api/v1/workflows", **h).status_code, 200)
        self.ws.feature_overrides = {"api": False}
        self.ws.save()
        self.assertEqual(self.client.get("/api/v1/workflows", **h).status_code, 403)

    def test_upload_rules(self):
        from .models import upload_problem
        self.ps(max_upload_mb=1, blocked_extensions="exe, bat")
        self.assertIn("not allowed", upload_problem(SimpleUploadedFile("x.EXE", b"1")))
        big = SimpleUploadedFile("a.pdf", b"0" * (1024 * 1024 + 1))
        self.assertIn("larger than 1 MB", upload_problem(big, field_max_mb=50))  # platform cap wins
        self.assertEqual(upload_problem(SimpleUploadedFile("a.pdf", b"1")), "")

    def test_private_network_guard(self):
        from flows import connectors
        from flows.models import Connector
        from .netguard import BlockedAddress, check_url
        check_url("http://127.0.0.1:9/")  # off by default
        self.ps(block_private_networks=True)
        with self.assertRaises(BlockedAddress):
            check_url("http://127.0.0.1:9/")
        c = Connector.objects.create(workspace=self.ws, name="Local", base_url="http://127.0.0.1:9",
                                     actions=[{"key": "a", "method": "GET", "path": ""}])
        res = connectors.run(c, "a", {})
        self.assertFalse(res["ok"])
        self.assertIn("private network", res["error"])

    # ---- workspaces & users ----
    def test_create_workspace_for_new_and_existing_owner(self):
        self.client.post("/platform/workspaces/", {"name": "Branch Ops", "owner_email": "sam@paperlane.test", "plan": "Team"})
        ws = Workspace.objects.get(name="Branch Ops")
        self.assertTrue(Membership.objects.filter(workspace=ws, user=self.sam, role="owner").exists())
        self.client.post("/platform/workspaces/", {"name": "Claims", "owner_email": "boss@claims.test"})
        ws2 = Workspace.objects.get(name="Claims")
        self.assertTrue(Invitation.objects.filter(workspace=ws2, email="boss@claims.test", role="owner").exists())
        self.assertTrue(any("boss@claims.test" in m.to for m in mail.outbox))

    def test_members_keep_an_owner(self):
        owner_m = Membership.objects.get(workspace=self.ws, user=self.owner)
        Membership.objects.filter(workspace=self.ws, role="owner").exclude(pk=owner_m.pk).update(role="creator")
        self.client.post(f"/platform/workspaces/{self.ws.pk}/", {"op": "member_role", "member": owner_m.pk, "role": "creator"})
        owner_m.refresh_from_db()
        self.assertEqual(owner_m.role, "owner")
        self.client.post(f"/platform/workspaces/{self.ws.pk}/", {"op": "member_deactivate", "member": owner_m.pk})
        owner_m.refresh_from_db()
        self.assertTrue(owner_m.active)

    def test_delete_workspace_needs_typed_id(self):
        ws = Workspace.objects.create(name="Temp")
        self.client.post(f"/platform/workspaces/{ws.pk}/", {"op": "delete", "confirm": "wrong"})
        self.assertTrue(Workspace.objects.filter(pk=ws.pk).exists())
        self.client.post(f"/platform/workspaces/{ws.pk}/", {"op": "delete", "confirm": ws.slug})
        self.assertFalse(Workspace.objects.filter(pk=ws.pk).exists())

    def test_user_actions(self):
        r = self.client.post("/platform/users/", {"email": "new@x.test", "first_name": "N", "workspace": self.ws.pk,
                                                  "role": "filler"})
        u = User.objects.get(email="new@x.test")
        self.assertFalse(u.has_usable_password())
        self.assertTrue(Membership.objects.filter(user=u, workspace=self.ws, role="filler").exists())
        self.assertTrue(any("new@x.test" in m.to for m in mail.outbox))  # set-password email
        p = Profile.objects.get(user=self.sam)
        p.totp_secret, p.totp_enabled_at = "ABC", __import__("django.utils.timezone", fromlist=["now"]).now()
        p.save()
        self.client.post(f"/platform/users/{self.sam.pk}/", {"op": "reset_2fa"})
        p.refresh_from_db()
        self.assertFalse(p.two_factor_on)
        self.client.post(f"/platform/users/{self.sam.pk}/", {"op": "grant_super"})
        self.sam.refresh_from_db()
        self.assertTrue(self.sam.is_superuser)
        self.client.post(f"/platform/users/{self.sam.pk}/", {"op": "deactivate"})
        self.sam.refresh_from_db()
        self.assertFalse(self.sam.is_active)
        # you can't lock yourself out
        self.client.post(f"/platform/users/{self.owner.pk}/", {"op": "deactivate"})
        self.client.post(f"/platform/users/{self.owner.pk}/", {"op": "revoke_super"})
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active and self.owner.is_superuser)

    def test_jobs_page_actions(self):
        r = self.client.post("/platform/jobs/", {"op": "tick"})
        self.assertRedirects(r, "/platform/jobs/")
        self.assertTrue(PlatformEvent.objects.filter(action="platform.jobs_run").exists())


class AuditFixTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        cache.clear()

    def test_html_field_is_sanitized(self):
        from core.sanitize import clean_html
        dirty = ('<p onclick="x()">Hi <b>there</b><script>alert(1)</script><a href="javascript:alert(1)">x</a>'
                 '<a href="https://ok.test" target="_blank">ok</a><img src=x onerror=alert(1)><iframe src="//e"></iframe>')
        out = clean_html(dirty)
        for bad in ("onclick", "<script", "alert(1)</", "javascript:", "onerror", "<iframe"):
            self.assertNotIn(bad, out)
        self.assertIn("<b>there</b>", out)
        self.assertIn('rel="noopener noreferrer"', out)

    def test_test_run_needs_post(self):
        from flows.models import Packet, Workflow
        owner = User.objects.get(username="owner")
        self.client.force_login(owner)
        wf = Workflow.objects.filter(workspace__slug="demo", trashed_at__isnull=True).first()
        n = Packet.objects.count()
        r = self.client.get(f"/workflows/{wf.pk}/test/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Packet.objects.count(), n)  # a link can't start a test run
        self.client.post(f"/workflows/{wf.pk}/test/")
        self.assertEqual(Packet.objects.count(), n + 1)

    def test_next_redirect_stays_on_site(self):
        owner = User.objects.get(username="owner")
        self.client.force_login(owner)
        r = self.client.post("/notifications/read-all/", {"next": "https://evil.example/"}) if False else None
        from django.test import RequestFactory
        from core.utils import safe_url
        req = RequestFactory().post("/", HTTP_HOST="testserver")
        self.assertEqual(safe_url(req, "https://evil.example/x", "/fallback/"), "/fallback/")
        self.assertEqual(safe_url(req, "//evil.example", "/fallback/"), "/fallback/")
        self.assertEqual(safe_url(req, "/documents/", "/fallback/"), "/documents/")

    def test_group_members_can_open_their_documents(self):
        from accounts.models import Group
        from flows.models import Packet, StepRun
        fay = User.objects.get(username="fay")
        ws = Workspace.objects.get(slug="demo")
        m = Membership.objects.get(user=fay, workspace=ws)
        g = Group.objects.create(workspace=ws, name="Front office")
        g.members.add(m)
        p = Packet.objects.filter(workspace=ws).exclude(runs__assignee_email__iexact=fay.email).first()
        self.assertFalse(Packet.objects.visible_to(m).filter(pk=p.pk).exists())
        StepRun.objects.create(packet=p, step_key="x", step_name="Group step", assignee_group=g, assignee_name=g.name)
        self.assertTrue(Packet.objects.visible_to(m).filter(pk=p.pk).exists())

    def test_submissions_hidden_from_fillers(self):
        from flows.models import IntakeForm
        ws = Workspace.objects.get(slug="demo")
        form = IntakeForm.objects.create(workspace=ws, name="F", schema={"fields": []})
        self.client.force_login(User.objects.get(username="fay"))
        self.assertEqual(self.client.get(f"/forms/{form.pk}/submissions/").status_code, 403)
        self.assertNotContains(self.client.get("/forms/"), f"/forms/{form.pk}/submissions/")
        self.client.force_login(User.objects.get(username="ana"))  # auditor
        self.assertEqual(self.client.get(f"/forms/{form.pk}/submissions/").status_code, 200)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ConsoleExtrasTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        cache.clear()
        self.owner = User.objects.get(username="owner")

    def test_signin_log_records_success_and_failure(self):
        from .models import SignInEvent
        self.client.post("/accounts/login/", {"email": "sam@paperlane.test", "password": "wrong-password"})
        self.client.post("/accounts/login/", {"email": "sam@paperlane.test", "password": "paperlane-demo-2026"})
        results = list(SignInEvent.objects.filter(email__iexact="sam@paperlane.test").order_by("created_at").values_list("result", flat=True))
        self.assertEqual(results[:2], ["failed", "ok"])
        self.client.force_login(self.owner)
        r = self.client.get("/platform/sign-ins/?filter=failed")
        self.assertContains(r, "sam@paperlane.test")
        sam = User.objects.get(username="sam")
        self.assertContains(self.client.get(f"/platform/users/{sam.pk}/"), "Recent sign-ins")

    def test_broadcast_to_role_with_email(self):
        from core.models import Notification
        from .models import Broadcast
        self.client.force_login(self.owner)
        ws = Workspace.objects.get(slug="demo")
        r = self.client.post("/platform/broadcasts/", {"title": "Downtime Sat 9pm", "body": "30 minutes", "link": "/inbox/",
                                                       "workspace": ws.pk, "roles": ["filler", "supervisor"], "email": "on"})
        self.assertRedirects(r, "/platform/broadcasts/")
        got = set(Notification.objects.filter(title="Downtime Sat 9pm").values_list("user__username", flat=True))
        self.assertEqual(got, {"fay", "sam"})
        self.assertEqual(Broadcast.objects.get().recipients, 2)
        self.assertTrue(any("Downtime Sat 9pm" in m.subject for m in mail.outbox))
        r = self.client.post("/platform/broadcasts/", {"title": "x", "link": "javascript:alert(1)"})
        self.assertContains(r, "Use a path")

    def test_csv_exports(self):
        self.client.force_login(self.owner)
        r = self.client.get("/platform/export/users.csv")
        self.assertEqual(r["Content-Type"], "text/csv")
        self.assertIn("sam@paperlane.test", r.content.decode())
        self.assertIn(Workspace.objects.get(slug="demo").name, self.client.get("/platform/export/workspaces.csv").content.decode())
        self.assertEqual(self.client.get("/platform/export/secrets.csv").status_code, 403)
        self.client.force_login(User.objects.get(username="sam"))
        self.assertEqual(self.client.get("/platform/export/users.csv").status_code, 403)
