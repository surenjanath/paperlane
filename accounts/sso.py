"""Single sign-on with OpenID Connect (authorization code flow + PKCE), no extra libraries.

start:    discovery → state, nonce and PKCE verifier in the session → redirect to the provider.
callback: check state → exchange the code at the token endpoint (TLS, client authentication, PKCE) → read the user's
          claims from the userinfo endpoint with the access token → match or (if allowed) create the account.

Because the tokens come straight from the provider's token endpoint over TLS, the ID token doesn't need a separate
signature check (OIDC Core 3.1.3.7); the nonce returned in it is still compared when present."""
import base64
import hashlib
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse

from .models import Membership, Profile, SSOProvider

User = get_user_model()
SESSION = "sso_pending"


class SSOError(Exception):
    pass


def _http_json(url, data=None, headers=None):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 - admin-configured provider
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        raise SSOError(f"{url.split('/')[2]} answered {exc.code}: {exc.read()[:200].decode(errors='replace')}")
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise SSOError(f"Couldn't reach {url.split('/')[2]} ({exc})")


def discovery(provider):
    key = f"oidc-disc:{provider.issuer}"
    d = cache.get(key)
    if d is None:
        d = _http_json(provider.issuer.rstrip("/") + "/.well-known/openid-configuration")
        for k in ("authorization_endpoint", "token_endpoint"):
            if not d.get(k):
                raise SSOError(f"The provider's discovery document has no {k}.")
        cache.set(key, d, 3600)
    return d


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _claims_from_id_token(token):
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


def start(request, pk):
    provider = get_object_or_404(SSOProvider, pk=pk, enabled=True)
    try:
        d = discovery(provider)
    except SSOError as exc:
        messages.error(request, f"Single sign-on is unavailable: {exc}")
        return redirect("accounts:login")
    verifier = _b64(secrets.token_bytes(32))
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    request.session[SESSION] = {"provider": provider.pk, "state": state, "nonce": nonce, "verifier": verifier,
                                "next": request.GET.get("next", "")}
    params = {"response_type": "code", "client_id": provider.client_id, "redirect_uri": request.build_absolute_uri(reverse("accounts:sso_callback")),
              "scope": provider.scopes or "openid email profile", "state": state, "nonce": nonce,
              "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"}
    if provider.kind == "google":
        params["prompt"] = "select_account"
    return redirect(d["authorization_endpoint"] + ("&" if "?" in d["authorization_endpoint"] else "?") + urllib.parse.urlencode(params))


def _account_for(provider, claims):
    email = (claims.get("email") or claims.get("preferred_username") or claims.get("upn") or "").strip().lower()
    if "@" not in email:
        raise SSOError("Your identity provider didn't share an email address.")
    if claims.get("email_verified") is False:
        raise SSOError("Your email address isn't verified with your identity provider.")
    domain = email.rsplit("@", 1)[1]
    allowed = provider.domains()
    if not allowed:  # never trust an email claim from a provider for domains it wasn't explicitly configured for
        raise SSOError(f"{provider.name} isn't set up yet: an administrator must list its allowed email domains.")
    if domain not in allowed:
        raise SSOError(f"{domain} accounts can't sign in with {provider.name}.")
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        if not provider.create_users:
            raise SSOError("There's no account for this email. Ask your administrator for an invitation.")
        base = email.split("@")[0][:120] or "user"
        username, n = base, 1
        while User.objects.filter(username=username).exists():
            n += 1
            username = f"{base}{n}"
        user = User.objects.create_user(username=username, email=email, first_name=(claims.get("given_name") or "")[:150],
                                        last_name=(claims.get("family_name") or "")[:150])
        user.set_unusable_password()
        user.save()
        Profile.objects.get_or_create(user=user)
    if not user.is_active:
        raise SSOError("This account is deactivated.")
    if provider.workspace_id and provider.create_users:
        role = provider.default_role if provider.default_role in dict(Membership.ROLES) and provider.default_role != Membership.OWNER else Membership.FILLER
        Membership.objects.get_or_create(user=user, workspace_id=provider.workspace_id, defaults={"role": role})
    return user


def callback(request):
    from .security_views import finish_login

    pending = request.session.pop(SESSION, None)
    if not pending:
        raise Http404
    if request.GET.get("error"):
        messages.error(request, f"Sign-in was cancelled or refused: {request.GET.get('error_description') or request.GET['error']}")
        return redirect("accounts:login")
    if not secrets.compare_digest(request.GET.get("state", ""), pending["state"]):
        messages.error(request, "That sign-in link expired or was tampered with. Please try again.")
        return redirect("accounts:login")
    provider = get_object_or_404(SSOProvider, pk=pending["provider"], enabled=True)
    try:
        d = discovery(provider)
        tokens = _http_json(d["token_endpoint"], {
            "grant_type": "authorization_code", "code": request.GET.get("code", ""), "code_verifier": pending["verifier"],
            "redirect_uri": request.build_absolute_uri(reverse("accounts:sso_callback")),
            "client_id": provider.client_id, "client_secret": provider.client_secret})
        idc = _claims_from_id_token(tokens.get("id_token", ""))
        if idc.get("nonce") and not secrets.compare_digest(str(idc["nonce"]), pending["nonce"]):
            raise SSOError("The sign-in response didn't match this request.")
        if idc.get("aud") and provider.client_id not in (idc["aud"] if isinstance(idc["aud"], list) else [idc["aud"]]):
            raise SSOError("The sign-in response was issued for a different application.")
        claims = dict(idc)
        if d.get("userinfo_endpoint") and tokens.get("access_token"):
            claims.update(_http_json(d["userinfo_endpoint"], headers={"Authorization": f"Bearer {tokens['access_token']}"}))
        user = _account_for(provider, claims)
    except SSOError as exc:
        messages.error(request, str(exc))
        return redirect("accounts:login")
    from flows.engine import audit

    ws = provider.workspace or getattr(Membership.objects.filter(user=user, active=True).first(), "workspace", None)
    if ws:
        audit(ws, "security.sso_login", f"{user.email} via {provider.name}", actor=user, request=request)
    return finish_login(request, user, True, pending.get("next") or "")
