"""OAuth Dynamic Client Registration (RFC 7591) for MCP connectors.

ChatGPT and Claude connectors register themselves instead of an admin
creating a client. Contract:
  - Discovery advertises ``registration_endpoint`` while the instance allows it.
  - A self-registered client is MCP-only, never trusted, and owned by no org:
    the member picks the org at consent, and the token is pinned to it.
  - The client is held to the token_endpoint_auth_method it registered.
  - An org can turn self-registered clients off; that cuts off existing ones.
  - Org admins see self-registered clients connected to their org and can
    revoke them for their org only; they cannot edit/delete/rotate them.
"""

import base64
import hashlib
import secrets
import uuid
from urllib.parse import parse_qs, urlparse

import pytest

from app.settings.config import settings as bow_settings

CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
CHATGPT_CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"


def _pkce_pair():
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _headers(token, org_id=None):
    headers = {"Authorization": f"Bearer {token}"}
    if org_id:
        headers["X-Organization-Id"] = str(org_id)
    return headers


@pytest.fixture
def multi_org(monkeypatch):
    flags = bow_settings.bow_config.features
    monkeypatch.setattr(flags, "allow_multiple_organizations", True)
    monkeypatch.setattr(flags, "allow_uninvited_signups", True)


@pytest.fixture
def oauth_cfg(monkeypatch):
    cfg = bow_settings.bow_config.oauth_server

    def _set(**values):
        for key, value in values.items():
            monkeypatch.setattr(cfg, key, value)

    return _set


@pytest.fixture
def admin(enable_mcp, create_user, login_user, whoami):
    """A user who is admin of exactly one org with MCP on."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    enable_mcp(user_token=token, org_id=org_id)
    return {"token": token, "org_id": org_id}


@pytest.fixture
def register(test_client):
    def _register(**metadata):
        body = {"client_name": "Claude", "redirect_uris": [CLAUDE_CALLBACK]}
        body.update(metadata)
        return test_client.post("/api/oauth/register", json=body)
    return _register


def _consent(test_client, client_id, user_token, *, redirect_uri=CLAUDE_CALLBACK,
             organization_id=None, scope="mcp", active_org=None):
    verifier, challenge = _pkce_pair()
    body = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": "st",
        "scope": scope,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if organization_id:
        body["organization_id"] = str(organization_id)
    response = test_client.post(
        "/api/oauth/authorize", json=body, headers=_headers(user_token, active_org),
    )
    return response, verifier


def _code(consent_response):
    return parse_qs(urlparse(consent_response.json()["redirect_url"]).query)["code"][0]


def _exchange(test_client, client_id, code, verifier, *, secret=None, basic=False,
              redirect_uri=CLAUDE_CALLBACK):
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    headers = {}
    if basic:
        raw = f"{client_id}:{secret}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
    else:
        data["client_id"] = client_id
        if secret is not None:
            data["client_secret"] = secret
    return test_client.post("/api/oauth/token", data=data, headers=headers)


def _connect(test_client, client, user_token, **consent_kwargs):
    """Consent + exchange for a self-registered public client."""
    consent, verifier = _consent(test_client, client["client_id"], user_token, **consent_kwargs)
    assert consent.status_code == 200, consent.json()
    redirect_uri = consent_kwargs.get("redirect_uri", CLAUDE_CALLBACK)
    tokens = _exchange(
        test_client, client["client_id"], _code(consent), verifier,
        secret=client.get("client_secret"), redirect_uri=redirect_uri,
    )
    assert tokens.status_code == 200, tokens.json()
    return tokens.json()


def _mcp(test_client, access_token):
    return test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": f"Bearer {access_token}"},
    )


def _token_org(access_token):
    import asyncio

    from app.services.oauth_server_service import OAuthServerService
    from app.settings.database import create_async_session_factory

    async def _run():
        async with create_async_session_factory()() as db:
            result = await OAuthServerService().validate_access_token(db, access_token)
            return None if result is None else str(result[1].id)

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run())
    finally:
        loop.close()


# ── Discovery & registration ────────────────────────────────────────

@pytest.mark.e2e
def test_discovery_advertises_registration_only_while_enabled(test_client, oauth_cfg, register):
    metadata = test_client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["registration_endpoint"].endswith("/api/oauth/register")
    assert {"none", "client_secret_basic", "client_secret_post"} <= set(
        metadata["token_endpoint_auth_methods_supported"]
    )

    oauth_cfg(allow_dynamic_client_registration=False)
    metadata = test_client.get("/.well-known/oauth-authorization-server").json()
    assert "registration_endpoint" not in metadata
    assert register().status_code == 403


@pytest.mark.e2e
@pytest.mark.parametrize("auth_method,expects_secret", [
    ("none", False),
    ("client_secret_post", True),
    ("client_secret_basic", True),
    (None, True),  # RFC 7591 default is client_secret_basic
])
def test_registration_returns_credentials_for_the_requested_auth_method(
    register, auth_method, expects_secret,
):
    metadata = {"redirect_uris": [CHATGPT_CALLBACK]}
    if auth_method:
        metadata["token_endpoint_auth_method"] = auth_method
    response = register(**metadata)
    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["client_id"]
    assert body["redirect_uris"] == [CHATGPT_CALLBACK]
    assert body["token_endpoint_auth_method"] == (auth_method or "client_secret_basic")
    assert ("client_secret" in body) is expects_secret
    assert "refresh_token" in body["grant_types"]


@pytest.mark.e2e
def test_registration_grants_only_the_mcp_scope(register, test_client):
    body = register(scope="app mcp offline_access", token_endpoint_auth_method="none").json()
    assert body["scope"] == "mcp"
    info = test_client.get(f"/api/oauth/clients/{body['client_id']}/info").json()
    assert info["scopes"] == ["mcp"]
    assert info["trusted"] is False
    assert info["dynamic"] is True


@pytest.mark.e2e
@pytest.mark.parametrize("metadata", [
    {"redirect_uris": []},
    {"redirect_uris": "https://claude.ai/cb"},
    {"redirect_uris": ["http://evil.example.com/cb"]},
    {"redirect_uris": ["https://claude.ai/cb#frag"]},
    {"redirect_uris": ["javascript:alert(1)"]},
    {"redirect_uris": [f"https://a{i}.example.com/cb" for i in range(11)]},
    {"token_endpoint_auth_method": "private_key_jwt"},
    {"grant_types": ["client_credentials"]},
    {"response_types": ["token"]},
])
def test_registration_rejects_invalid_metadata(register, metadata):
    response = register(**metadata)
    assert response.status_code == 400
    assert response.json()["error"] in {"invalid_client_metadata", "invalid_redirect_uri"}


@pytest.mark.e2e
@pytest.mark.parametrize("uri", ["http://localhost:6274/cb", "http://127.0.0.1:33418/callback"])
def test_registration_allows_loopback_http_for_local_clients(register, uri):
    assert register(redirect_uris=[uri]).status_code == 201


@pytest.mark.e2e
def test_registration_is_rate_limited_per_ip(register, oauth_cfg, monkeypatch):
    from app.routes import oauth_server as routes
    monkeypatch.setattr(routes, "_registration_limiter", routes._RegistrationRateLimiter())
    oauth_cfg(dynamic_registrations_per_ip_per_hour=3)
    statuses = [register().status_code for _ in range(5)]
    assert statuses[:3] == [201, 201, 201]
    assert statuses[3:] == [429, 429]


# ── Sign-in with a self-registered client ───────────────────────────

@pytest.mark.e2e
@pytest.mark.parametrize("callback", [CLAUDE_CALLBACK, CHATGPT_CALLBACK])
def test_public_client_full_flow_reaches_mcp(register, admin, test_client, callback):
    client = register(redirect_uris=[callback], token_endpoint_auth_method="none").json()
    tokens = _connect(test_client, client, admin["token"], redirect_uri=callback)
    assert _mcp(test_client, tokens["access_token"]).status_code == 200
    assert _token_org(tokens["access_token"]) == str(admin["org_id"])

    refreshed = test_client.post("/api/oauth/token", data={
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": client["client_id"],
    })
    assert refreshed.status_code == 200, refreshed.json()
    assert _mcp(test_client, refreshed.json()["access_token"]).status_code == 200


@pytest.mark.e2e
def test_confidential_client_must_authenticate_with_its_secret(register, admin, test_client):
    client = register(token_endpoint_auth_method="client_secret_basic").json()

    for secret, basic in [(None, False), ("bow_secret_wrong", True), ("bow_secret_wrong", False)]:
        consent, verifier = _consent(test_client, client["client_id"], admin["token"])
        denied = _exchange(test_client, client["client_id"], _code(consent), verifier,
                           secret=secret, basic=basic)
        assert denied.status_code == 400, (secret, basic)
        assert denied.json()["error"] == "invalid_grant"

    consent, verifier = _consent(test_client, client["client_id"], admin["token"])
    ok = _exchange(test_client, client["client_id"], _code(consent), verifier,
                   secret=client["client_secret"], basic=True)
    assert ok.status_code == 200, ok.json()
    assert _mcp(test_client, ok.json()["access_token"]).status_code == 200


@pytest.mark.e2e
def test_self_registered_client_cannot_request_the_app_scope(register, admin, test_client):
    client = register(token_endpoint_auth_method="none").json()
    consent, _ = _consent(test_client, client["client_id"], admin["token"], scope="app")
    assert consent.status_code == 400
    redirect = test_client.get("/api/oauth/authorize", params={
        "client_id": client["client_id"], "redirect_uri": CLAUDE_CALLBACK,
        "response_type": "code", "scope": "app",
        "code_challenge": "x" * 43, "code_challenge_method": "S256",
    }, follow_redirects=False)
    assert redirect.status_code == 400


@pytest.mark.e2e
def test_consent_rejects_unregistered_redirect_uri(register, admin, test_client):
    client = register(token_endpoint_auth_method="none").json()
    consent, _ = _consent(test_client, client["client_id"], admin["token"],
                          redirect_uri="https://attacker.example.com/cb")
    assert consent.status_code == 400


# ── Org choice at consent ───────────────────────────────────────────

@pytest.mark.e2e
def test_multi_org_user_must_pick_an_org_and_token_binds_to_it(
    multi_org, register, admin, create_organization, enable_mcp, test_client,
):
    org_b = create_organization(name=f"B {uuid.uuid4().hex[:6]}", user_token=admin["token"])
    enable_mcp(user_token=admin["token"], org_id=org_b)
    client = register(token_endpoint_auth_method="none").json()

    unspecified, _ = _consent(test_client, client["client_id"], admin["token"])
    assert unspecified.status_code == 400

    # The active-org header must not decide it either: the chosen org does.
    for chosen, active in [(org_b, admin["org_id"]), (admin["org_id"], org_b)]:
        tokens = _connect(test_client, client, admin["token"],
                          organization_id=chosen, active_org=active)
        assert _token_org(tokens["access_token"]) == str(chosen)


@pytest.mark.e2e
def test_user_cannot_bind_a_self_registered_client_to_someone_elses_org(
    multi_org, register, admin, create_user, login_user, test_client,
):
    outsider = create_user(email=f"outsider_{uuid.uuid4().hex[:8]}@test.com")
    outsider_token = login_user(outsider["email"], outsider["password"])
    client = register(token_endpoint_auth_method="none").json()

    consent, _ = _consent(test_client, client["client_id"], outsider_token,
                          organization_id=admin["org_id"])
    assert consent.status_code == 403


@pytest.mark.e2e
def test_static_client_rejects_a_different_org_in_consent(
    multi_org, admin, create_oauth_client, create_organization, test_client,
):
    other_org = create_organization(name=f"O {uuid.uuid4().hex[:6]}", user_token=admin["token"])
    client = create_oauth_client(user_token=admin["token"], org_id=admin["org_id"])
    consent, _ = _consent(test_client, client["client_id"], admin["token"],
                          organization_id=other_org)
    assert consent.status_code == 400


# ── Org and instance switches ───────────────────────────────────────

@pytest.mark.e2e
def test_org_switch_blocks_new_consents_and_cuts_off_existing_connections(
    register, admin, update_organization_settings, test_client,
):
    client = register(token_endpoint_auth_method="none").json()
    tokens = _connect(test_client, client, admin["token"])
    assert _mcp(test_client, tokens["access_token"]).status_code == 200

    update_organization_settings(
        config={"allow_dynamic_oauth_clients": {"value": False}},
        user_token=admin["token"], org_id=admin["org_id"],
    )
    consent, _ = _consent(test_client, client["client_id"], admin["token"])
    assert consent.status_code == 403
    assert _mcp(test_client, tokens["access_token"]).status_code == 401
    refresh = test_client.post("/api/oauth/token", data={
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": client["client_id"],
    })
    assert refresh.status_code == 400


@pytest.mark.e2e
def test_org_switch_does_not_affect_admin_created_clients(
    admin, create_oauth_client, update_organization_settings, test_client,
):
    update_organization_settings(
        config={"allow_dynamic_oauth_clients": {"value": False}},
        user_token=admin["token"], org_id=admin["org_id"],
    )
    client = create_oauth_client(user_token=admin["token"], org_id=admin["org_id"])
    tokens = _connect(test_client, client, admin["token"])
    assert _mcp(test_client, tokens["access_token"]).status_code == 200


@pytest.mark.e2e
def test_instance_switch_cuts_off_existing_self_registered_clients(
    register, admin, oauth_cfg, test_client,
):
    client = register(token_endpoint_auth_method="none").json()
    tokens = _connect(test_client, client, admin["token"])

    oauth_cfg(allow_dynamic_client_registration=False)
    assert _mcp(test_client, tokens["access_token"]).status_code == 401
    consent, _ = _consent(test_client, client["client_id"], admin["token"])
    assert consent.status_code == 400


# ── Admin visibility and revocation ─────────────────────────────────

def _invite_member(test_client, create_user, login_user, admin):
    email = f"member_{uuid.uuid4().hex[:10]}@test.com"
    invite = test_client.post(
        f"/api/organizations/{admin['org_id']}/members",
        json={"organization_id": admin["org_id"], "email": email, "role": "member"},
        headers=_headers(admin["token"], admin["org_id"]),
    )
    assert invite.status_code == 200, invite.json()
    create_user(email=email, password="Test1234!")
    return login_user(email, "Test1234!")


@pytest.mark.e2e
def test_admin_sees_connected_self_registered_client_and_can_revoke_it(
    multi_org, register, admin, list_oauth_clients, create_user, login_user,
    enable_mcp, test_client,
):
    client = register(token_endpoint_auth_method="none").json()
    member_token = _invite_member(test_client, create_user, login_user, admin)

    # Not connected to the org yet: not the org's business.
    assert all(c["client_id"] != client["client_id"]
               for c in list_oauth_clients(user_token=admin["token"], org_id=admin["org_id"]))

    member_tokens = _connect(test_client, client, member_token, organization_id=admin["org_id"])

    # A different org connected to the same client is unaffected by the revoke.
    other = create_user(email=f"other_{uuid.uuid4().hex[:8]}@test.com")
    other_token = login_user(other["email"], other["password"])
    other_org = test_client.post("/api/organizations", json={"name": f"x{uuid.uuid4().hex[:6]}"},
                                 headers=_headers(other_token)).json()["id"]
    enable_mcp(user_token=other_token, org_id=other_org)
    other_tokens = _connect(test_client, client, other_token, organization_id=other_org)

    listed = list_oauth_clients(user_token=admin["token"], org_id=admin["org_id"])
    row = next(c for c in listed if c["client_id"] == client["client_id"])
    assert row["dynamic"] is True
    assert row["active_token_count"] == 1  # only this org's connection

    # Members cannot list or revoke; admins cannot edit/rotate/delete it.
    member_headers = _headers(member_token, admin["org_id"])
    assert test_client.get("/api/oauth/clients", headers=member_headers).status_code == 403
    assert test_client.post(f"/api/oauth/clients/{row['id']}/revoke",
                            headers=member_headers).status_code == 403
    admin_headers = _headers(admin["token"], admin["org_id"])
    assert test_client.patch(f"/api/oauth/clients/{row['id']}", json={"trusted": True},
                             headers=admin_headers).status_code == 404
    assert test_client.post(f"/api/oauth/clients/{row['id']}/rotate",
                            headers=admin_headers).status_code == 404
    assert test_client.delete(f"/api/oauth/clients/{row['id']}",
                              headers=admin_headers).status_code == 404

    revoked = test_client.post(f"/api/oauth/clients/{row['id']}/revoke", headers=admin_headers)
    assert revoked.status_code == 200, revoked.json()
    assert _mcp(test_client, member_tokens["access_token"]).status_code == 401
    assert _mcp(test_client, other_tokens["access_token"]).status_code == 200
    assert all(c["client_id"] != client["client_id"]
               for c in list_oauth_clients(user_token=admin["token"], org_id=admin["org_id"]))


@pytest.mark.e2e
def test_admin_cannot_revoke_a_self_registered_client_not_connected_to_their_org(
    register, admin, test_client,
):
    client = register(token_endpoint_auth_method="none").json()
    import asyncio

    from sqlalchemy import select

    from app.models.oauth_server import OAuthClient
    from app.settings.database import create_async_session_factory

    async def _db_id():
        async with create_async_session_factory()() as db:
            return (await db.execute(
                select(OAuthClient.id).where(OAuthClient.client_id == client["client_id"])
            )).scalar_one()

    loop = asyncio.new_event_loop()
    try:
        db_id = loop.run_until_complete(_db_id())
    finally:
        loop.close()
    response = test_client.post(f"/api/oauth/clients/{db_id}/revoke",
                                headers=_headers(admin["token"], admin["org_id"]))
    assert response.status_code == 404


@pytest.mark.e2e
def test_abandoned_registrations_are_swept_but_connected_ones_kept(
    register, admin, test_client, oauth_cfg, monkeypatch,
):
    """Registration is unauthenticated, so registrations that never led to a
    sign-in must not pile up; ones a member connected must survive."""
    from datetime import datetime, timedelta

    from app.services import oauth_server_service

    oauth_cfg(dynamic_client_unused_ttl_days=7)
    abandoned = register(token_endpoint_auth_method="none").json()
    connected = register(token_endpoint_auth_method="none").json()
    tokens = _connect(test_client, connected, admin["token"])

    future = datetime.utcnow() + timedelta(days=8)
    monkeypatch.setattr(oauth_server_service, "_utcnow", lambda: future)
    assert register().status_code == 201  # sweeping happens on registration

    assert test_client.get(f"/api/oauth/clients/{abandoned['client_id']}/info").status_code == 404
    assert test_client.get(f"/api/oauth/clients/{connected['client_id']}/info").status_code == 200
    monkeypatch.undo()
    assert _mcp(test_client, tokens["access_token"]).status_code == 200


@pytest.mark.e2e
@pytest.mark.parametrize("tz", ["America/New_York", "Asia/Jerusalem"])
def test_registration_issued_at_is_current_unix_time(register, monkeypatch, tz):
    """Unix time regardless of the server's local timezone."""
    import time
    monkeypatch.setenv("TZ", tz)
    time.tzset()
    try:
        issued = register().json()["client_id_issued_at"]
    finally:
        monkeypatch.undo()
        time.tzset()
    assert abs(issued - time.time()) < 300
