"""Single sign-on (OIDC): start redirect with PKCE, callback checks, domain rules, account creation, admin page."""
import base64
import json
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase

from accounts.models import Membership, SSOProvider, Workspace

User = get_user_model()
DISC = {"authorization_endpoint": "https://idp.test/authorize", "token_endpoint": "https://idp.test/token",
        "userinfo_endpoint": "https://idp.test/userinfo"}


def id_token(**claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


class SSOTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        cache.clear()
        self.ws = Workspace.objects.get(slug="demo")
        self.p = SSOProvider.objects.create(name="Contoso", kind="microsoft", issuer="https://idp.test", client_id="cid",
                                            client_secret="sec", allowed_domains="contoso.test, paperlane.test",
                                            create_users=True, workspace=self.ws, default_role="creator")

    def fake(self, userinfo, nonce=None, aud="cid"):
        def http(url, data=None, headers=None):
            if url.endswith("openid-configuration"):
                return DISC
            if url == DISC["token_endpoint"]:
                self.token_req = data
                return {"access_token": "at", "id_token": id_token(nonce=nonce or self.nonce, aud=aud, email=userinfo.get("email"))}
            if url == DISC["userinfo_endpoint"]:
                return userinfo
            raise AssertionError(url)
        return mock.patch("accounts.sso._http_json", side_effect=http)

    def begin(self):
        with self.fake({}):
            r = self.client.get(f"/accounts/sso/{self.p.pk}/?next=/inbox/")
        q = parse_qs(urlparse(r["Location"]).query)
        self.assertEqual((q["code_challenge_method"][0], q["client_id"][0]), ("S256", "cid"))
        self.state, self.nonce = q["state"][0], q["nonce"][0]
        return q

    def test_login_button_and_existing_user_sign_in(self):
        self.assertContains(self.client.get("/accounts/login/"), "Sign in with Contoso")
        self.begin()
        with self.fake({"email": "sam@paperlane.test", "email_verified": True}):
            r = self.client.get(f"/accounts/sso/callback/?code=abc&state={self.state}")
        self.assertRedirects(r, "/inbox/", fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), User.objects.get(username="sam").pk)
        self.assertIn("code_verifier", self.token_req)  # PKCE sent

    def test_new_user_created_and_added_to_workspace(self):
        self.begin()
        with self.fake({"email": "New.Person@Contoso.test", "given_name": "New"}):
            self.client.get(f"/accounts/sso/callback/?code=abc&state={self.state}")
        u = User.objects.get(email="new.person@contoso.test")
        self.assertFalse(u.has_usable_password())
        self.assertEqual(Membership.objects.get(user=u, workspace=self.ws).role, "creator")

    def test_rejections(self):
        cases = [({"email": "x@evil.test"}, {}, "evil.test accounts"),                        # domain not allowed
                 ({"email": "a@contoso.test", "email_verified": False}, {}, "verified with your identity provider"),
                 ({"email": "a@contoso.test"}, {"nonce": "wrong"}, "match this request"),       # nonce replay
                 ({"email": "a@contoso.test"}, {"aud": "other-app"}, "different application")]
        for userinfo, kw, msg in cases:
            self.begin()
            with self.fake(userinfo, **kw):
                r = self.client.get(f"/accounts/sso/callback/?code=abc&state={self.state}", follow=True)
            self.assertContains(r, msg, msg_prefix=str(userinfo))
            self.assertNotIn("_auth_user_id", self.client.session)
        self.begin()
        r = self.client.get("/accounts/sso/callback/?code=abc&state=forged", follow=True)
        self.assertContains(r, "expired or was tampered")
        self.assertEqual(self.client.get("/accounts/sso/callback/?code=abc&state=x").status_code, 404)  # nothing pending

    def test_no_account_creation_unless_enabled_and_domains_required(self):
        self.p.create_users = False
        self.p.save()
        self.begin()
        with self.fake({"email": "stranger@contoso.test"}):
            r = self.client.get(f"/accounts/sso/callback/?code=abc&state={self.state}", follow=True)
        self.assertContains(r, "no account for this email")
        SSOProvider.objects.filter(pk=self.p.pk).update(allowed_domains="")
        self.begin()
        with self.fake({"email": "sam@paperlane.test"}):
            r = self.client.get(f"/accounts/sso/callback/?code=abc&state={self.state}", follow=True)
        self.assertContains(r, "allowed email domains")

    def test_admin_page(self):
        owner = User.objects.get(username="owner")
        owner.is_superuser = True
        owner.save()
        self.client.force_login(owner)
        self.assertContains(self.client.get("/platform/sso/"), "redirect URI")
        r = self.client.post("/platform/sso/", {"name": "G", "kind": "google", "issuer": "https://accounts.google.com", "client_id": "x",
                                                "scopes": "openid email profile", "allowed_domains": "not a domain", "default_role": "filler", "enabled": "on"})
        self.assertContains(r, "List the email domains")
        self.assertFalse(SSOProvider.objects.filter(name="G").exists())
