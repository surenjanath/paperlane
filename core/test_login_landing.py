"""Landing page and the development-only demo login."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

User = get_user_model()


class LoginLandingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def test_landing_renders(self):
        r = self.client.get("/welcome/")
        self.assertContains(r, "in one workflow")
        self.assertContains(r, "Ask what went wrong")
        self.assertEqual(r.content.decode().count("Field history"), 1)

    @override_settings(DEBUG=True)
    def test_demo_login_in_development(self):
        r = self.client.get("/accounts/login/")
        self.assertContains(r, "Demo accounts")
        for email in ("owner@paperlane.test", "sam@paperlane.test", "ana@paperlane.test", "cal@paperlane.test", "fay@paperlane.test"):
            self.assertContains(r, f'value="{email}"')
        r = self.client.post("/accounts/login/demo/", {"email": "sam@paperlane.test"})
        self.assertRedirects(r, "/", fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), User.objects.get(username="sam").pk)
        self.client.logout()
        r = self.client.post("/accounts/login/demo/?next=/inbox/", {"email": "fay@paperlane.test"})
        self.assertRedirects(r, "/inbox/", fetch_redirect_response=False)
        self.client.logout()
        # only the seeded demo users — never an arbitrary account
        other = User.objects.create_user("someone", "someone@corp.test", "x")
        self.assertEqual(self.client.post("/accounts/login/demo/", {"email": other.email}).status_code, 404)
        self.assertNotIn("_auth_user_id", self.client.session)

    @override_settings(DEBUG=False)
    def test_demo_login_off_in_production(self):
        with mock.patch.dict("os.environ", {"PAPERLANE_DEMO_LOGIN": ""}):
            self.assertNotContains(self.client.get("/accounts/login/"), "Demo accounts")
            self.assertEqual(self.client.post("/accounts/login/demo/", {"email": "owner@paperlane.test"}).status_code, 404)
        with mock.patch.dict("os.environ", {"PAPERLANE_DEMO_LOGIN": "1"}):
            self.assertContains(self.client.get("/accounts/login/"), "Demo accounts")
