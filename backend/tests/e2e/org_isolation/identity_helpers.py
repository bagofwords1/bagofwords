"""Shared helpers for the identity tests in this package.

Everything goes through the HTTP API. The only thing replaced is the identity
provider itself: outbound HTTP to the (fake) OIDC issuer is answered locally,
so the real callback route, account lookup and membership code all run.
"""
import uuid
from contextlib import contextmanager
from urllib.parse import parse_qs, urlparse

import httpx
import jwt as pyjwt

from app.settings.bow_config import OIDCProvider
from app.settings.config import settings as bow_settings

ISSUER = "https://idp.identity-tests.example"
PASSWORD = "test123"


def hdr(token, org_id=None):
    headers = {"Authorization": f"Bearer {token}"}
    if org_id:
        headers["X-Organization-Id"] = str(org_id)
    return headers


def unique_email(prefix="user"):
    return f"{prefix}_{uuid.uuid4().hex[:10]}@test.com"


def register(client, email, invite_token=None, password=PASSWORD):
    body = {"name": f"user_{uuid.uuid4().hex[:8]}", "email": email, "password": password}
    if invite_token:
        body["invite_token"] = invite_token
    return client.post("/api/auth/register", json=body)


def login(client, email, password=PASSWORD):
    return client.post("/api/auth/jwt/login", data={"username": email, "password": password})


def whoami(client, token):
    resp = client.get("/api/users/whoami", headers=hdr(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


def org_ids(client, token):
    return {o["id"] for o in whoami(client, token)["organizations"]}


def invite(client, admin_token, org_id, email, role="member"):
    """Invite ``email`` and return (membership_id, invite_token) as the admin sees them."""
    resp = client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": email, "role": role},
        headers=hdr(admin_token, org_id),
    )
    assert resp.status_code == 200, resp.text
    membership_id = resp.json()["id"]
    link = client.get(
        f"/api/organizations/{org_id}/members/{membership_id}/invite-link",
        headers=hdr(admin_token, org_id),
    )
    assert link.status_code == 200, link.text
    return membership_id, link.json()["token"]


def pending_invite_emails(client, admin_token, org_id):
    resp = client.get(f"/api/organizations/{org_id}/members", headers=hdr(admin_token, org_id))
    assert resp.status_code == 200, resp.text
    return {(m.get("email") or "").lower() for m in resp.json() if not m.get("user_id") and not m.get("user")}


def bootstrap_admin(client):
    """First user of the instance: owns the first organization."""
    email = unique_email("admin")
    assert register(client, email).status_code == 201
    token = login(client, email).json()["access_token"]
    return {"email": email, "token": token, "org_id": whoami(client, token)["organizations"][0]["id"],
            "id": whoami(client, token)["id"]}


def second_org_admin(client, first_admin):
    """A second organization with its own admin (needs multi-org enabled)."""
    email = unique_email("admin2")
    _, token = invite(client, first_admin["token"], first_admin["org_id"], email)
    assert register(client, email, invite_token=token).status_code == 201
    session = login(client, email).json()["access_token"]
    created = client.post("/api/organizations", json={"name": f"Org {uuid.uuid4().hex[:6]}"}, headers=hdr(session))
    assert created.status_code == 200, created.text
    return {"email": email, "token": session, "org_id": created.json()["id"]}


@contextmanager
def feature(name, value):
    flags = bow_settings.bow_config.features
    saved = getattr(flags, name)
    setattr(flags, name, value)
    try:
        yield
    finally:
        setattr(flags, name, saved)


@contextmanager
def auth_mode(mode):
    saved = bow_settings.bow_config.auth.mode
    bow_settings.bow_config.auth.mode = mode
    try:
        yield
    finally:
        bow_settings.bow_config.auth.mode = saved


def oidc_provider(name="corp", issuer=ISSUER, **options):
    return OIDCProvider(name=name, enabled=True, issuer=issuer, client_id="client", client_secret="secret", **options)


class FakeIdentityProvider:
    """Answers the OIDC discovery and token requests the callback makes."""

    def __init__(self, monkeypatch, providers):
        self.claims = {}
        monkeypatch.setattr(bow_settings.bow_config, "oidc_providers", providers)
        transport = httpx.MockTransport(self._respond)

        # Clients opened from here on reach the fake provider instead of the
        # network (the OpenID client reads discovery synchronously, the token
        # exchange is async). The test client already exists and is unaffected.
        def _offline(real_client):
            class _Client(real_client):
                def __init__(self, *args, **kwargs):
                    kwargs["transport"] = transport
                    super().__init__(*args, **kwargs)
            return _Client

        monkeypatch.setattr(httpx, "AsyncClient", _offline(httpx.AsyncClient))
        monkeypatch.setattr(httpx, "Client", _offline(httpx.Client))

    def _respond(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        issuer = url.split("/.well-known/")[0] if "/.well-known/" in url else None
        if issuer:
            return httpx.Response(200, json={
                "issuer": issuer,
                "authorization_endpoint": f"{issuer}/authorize",
                "token_endpoint": f"{issuer}/token",
                "userinfo_endpoint": f"{issuer}/userinfo",
            })
        if url.endswith("/token"):
            return httpx.Response(200, json={
                "access_token": "provider-access-token",
                "expires_in": 3600,
                "id_token": pyjwt.encode(self.claims, "not-verified-by-the-app-under-test", algorithm="HS256"),
            })
        return httpx.Response(404, json={})

    def sign_in(self, client, provider, email, subject=None, **extra_claims):
        """Complete a sign-in through ``provider`` as ``email``.

        Returns ("ok", session_token) or ("error", error_code) — the two
        outcomes the sign-in page can receive from the callback."""
        self.claims = {"sub": subject or f"sub-{email}", "email": email, **extra_claims}
        state = uuid.uuid4().hex
        resp = client.get(
            f"/api/auth/{provider}/callback",
            params={"code": "auth-code", "state": state},
            headers={"Cookie": f"oidc_{provider}_state={state}; oidc_{provider}_verifier=verifier"},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        query = parse_qs(urlparse(resp.headers["location"]).query)
        if "login_code" in query:
            exchanged = client.post("/api/auth/exchange", json={"login_code": query["login_code"][0]})
            assert exchanged.status_code == 200, exchanged.text
            return "ok", exchanged.json()["access_token"]
        return "error", query["error_code"][0]


def enable_domain_signup(client, monkeypatch, admin, domains):
    """Turn on the organization's domain sign-up policy (a licensed feature)."""
    from app.ee import license as ee_license
    monkeypatch.setattr(ee_license, "_cached_license", ee_license.LicenseInfo(
        licensed=True, tier="enterprise", org_name="identity-tests"))
    monkeypatch.setattr(ee_license, "_cache_initialized", True)
    resp = client.put(
        "/api/organization/signup-policy",
        json={"enabled": True, "allowed_domains": domains, "auto_invite_role": "member"},
        headers=hdr(admin["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.text
