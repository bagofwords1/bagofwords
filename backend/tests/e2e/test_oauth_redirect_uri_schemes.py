"""OAuth navigation targets never carry a script-executing scheme (pentest WP8).

* OAuth client redirect URIs: allowlist of https + loopback http. Everything
  else (javascript:, data:, custom app schemes, non-loopback http, relative,
  obfuscated) is refused on create and update, and a row stored before the
  check existed can never be redirected to.
* Connection OAuth authorize URLs: only absolute http(s) is accepted on save,
  and the authorize route refuses a pre-existing bad value.
"""

import base64
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.oauth_server import OAuthClient

DANGEROUS_REDIRECT_URIS = [
    "javascript:alert(document.domain)",
    " JaVaScRiPt:alert(1)",
    "JAVASCRIPT:alert(1)",
    "java\tscript:alert(1)",
    "java\nscript:alert(1)",
    "javascript​:alert(1)",
    "\x00javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
    "blob:https://evil.example/uuid",
    "about:blank",
    "/relative/callback",
    "//evil.example.com/cb",
    "https:/missing-host",
    "http://",
    "not a url",
    "cursor://anysphere.cursor-retrieval/oauth/callback",
    "vscode://vscode.github-authentication/did-authenticate",
    "com.example.desktop:/oauth2redirect",
    "http://bow.example.com/oauth/callback",
    "http://localhost.evil.com/cb",
]

ALLOWED_REDIRECT_URIS = [
    "https://app.example.com/oauth/callback",
    "http://localhost:6274/oauth/callback",
    "http://127.0.0.1:33418/callback",
    "http://[::1]:8976/callback",
]


def _pkce():
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _admin(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    return token, org_id, {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


# ── OAuth client redirect URIs ──────────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.parametrize("bad_uri", DANGEROUS_REDIRECT_URIS)
def test_dangerous_redirect_uri_rejected_on_create_and_update(
    bad_uri, test_client, create_user, login_user, whoami, create_oauth_client,
):
    token, org_id, headers = _admin(create_user, login_user, whoami)

    created = test_client.post(
        "/api/oauth/clients",
        json={"name": "Evil", "redirect_uris": ["https://ok.example.com/cb", bad_uri]},
        headers=headers,
    )
    assert created.status_code == 400

    client = create_oauth_client(user_token=token, org_id=org_id, redirect_uris=["https://ok.example.com/cb"])
    updated = test_client.patch(
        f"/api/oauth/clients/{client['id']}", json={"redirect_uris": [bad_uri]}, headers=headers,
    )
    assert updated.status_code == 400
    listed = test_client.get("/api/oauth/clients", headers=headers).json()
    stored = next(c for c in listed if c["id"] == client["id"])
    assert stored["redirect_uris"] == ["https://ok.example.com/cb"]


@pytest.mark.e2e
@pytest.mark.parametrize("redirect_uri", ALLOWED_REDIRECT_URIS)
def test_allowed_redirect_uri_registers_and_completes_auth_code_flow(
    redirect_uri, test_client, create_user, login_user, whoami, create_oauth_client,
):
    token, org_id, headers = _admin(create_user, login_user, whoami)
    client = create_oauth_client(user_token=token, org_id=org_id, redirect_uris=[redirect_uri])
    assert client["redirect_uris"] == [redirect_uri]
    _complete_auth_code_flow(test_client, headers, client, redirect_uri)


def _complete_auth_code_flow(test_client, headers, client, redirect_uri):
    # Consent entry point redirects to the in-app consent page.
    verifier, challenge = _pkce()
    entry = test_client.get(
        "/api/oauth/authorize",
        params={"client_id": client["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
                "scope": "mcp", "code_challenge": challenge, "code_challenge_method": "S256", "state": "s1"},
        follow_redirects=False,
    )
    assert entry.status_code == 302
    assert entry.headers["location"].startswith("/authorize?")

    approved = test_client.post(
        "/api/oauth/authorize",
        json={"client_id": client["client_id"], "redirect_uri": redirect_uri, "state": "s1", "scope": "mcp",
              "code_challenge": challenge, "code_challenge_method": "S256"},
        headers=headers,
    )
    assert approved.status_code == 200, approved.json()
    callback = approved.json()["redirect_url"]
    assert callback.split(":", 1)[0] == redirect_uri.split(":", 1)[0]
    code = parse_qs(urlparse(callback).query)["code"][0]

    tokens = test_client.post(
        "/api/oauth/token",
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
              "client_id": client["client_id"], "client_secret": client["client_secret"],
              "code_verifier": verifier},
    )
    assert tokens.status_code == 200, tokens.json()
    assert tokens.json()["access_token"].startswith("bow_oauth_")


@pytest.mark.e2e
def test_preexisting_dangerous_redirect_uri_is_never_used(
    test_client, create_user, login_user, whoami, create_oauth_client,
):
    token, org_id, headers = _admin(create_user, login_user, whoami)
    client = create_oauth_client(user_token=token, org_id=org_id, redirect_uris=["https://ok.example.com/cb"])
    bad = "javascript:alert(document.cookie)"

    async def plant_legacy_row():
        # A row saved before scheme validation existed cannot be produced
        # through the API any more, so it is written directly.
        async with async_session_maker() as db:
            row = await db.scalar(select(OAuthClient).where(OAuthClient.id == client["id"]))
            row.redirect_uris = json.dumps(["https://ok.example.com/cb", bad])
            await db.commit()

    import asyncio
    asyncio.run(plant_legacy_row())

    _, challenge = _pkce()
    entry = test_client.get(
        "/api/oauth/authorize",
        params={"client_id": client["client_id"], "redirect_uri": bad, "response_type": "code",
                "scope": "mcp", "code_challenge": challenge, "code_challenge_method": "S256"},
        follow_redirects=False,
    )
    assert entry.status_code == 400

    info = test_client.get(f"/api/oauth/clients/{client['client_id']}/info", params={"redirect_uri": bad})
    assert info.status_code == 400

    approved = test_client.post(
        "/api/oauth/authorize",
        json={"client_id": client["client_id"], "redirect_uri": bad, "scope": "mcp",
              "code_challenge": challenge, "code_challenge_method": "S256"},
        headers=headers,
    )
    assert approved.status_code == 400

    # The legitimate URI on the same client still works.
    ok = test_client.get(f"/api/oauth/clients/{client['client_id']}/info",
                         params={"redirect_uri": "https://ok.example.com/cb"})
    assert ok.status_code == 200


# ── Connection OAuth authorize URL ──────────────────────────────────────────

def _sap_payload(authorization_url):
    return {
        "name": f"SAP {secrets.token_hex(3)}",
        "type": "sap_datasphere",
        "config": {"host": "tenant.example.invalid", "token_url": "https://auth.example.com/oauth/token",
                   "authorization_url": authorization_url},
        "credentials": {"client_id": "cid", "client_secret": "csecret"},
        "auth_policy": "user_required",
        "allowed_user_auth_modes": ["oauth"],
    }


@pytest.mark.e2e
@pytest.mark.parametrize("bad_url", [
    "javascript:alert(1)", " JaVaScRiPt:alert(1)", "java\tscript:alert(1)",
    "data:text/html,x", "cursor://not-a-browser-target", "/relative/authorize",
])
def test_connection_authorization_url_must_be_http(bad_url, test_client, create_user, login_user, whoami):
    _, _, headers = _admin(create_user, login_user, whoami)
    resp = test_client.post("/api/connections", json=_sap_payload(bad_url), headers=headers)
    assert resp.status_code in (400, 422)


@pytest.mark.e2e
def test_connection_oauth_https_authorize_url_works_and_legacy_bad_row_is_refused(
    test_client, create_user, login_user, whoami,
):
    _, _, headers = _admin(create_user, login_user, whoami)
    good = "https://auth.example.com/oauth/authorize"
    created = test_client.post("/api/connections", json=_sap_payload(good), headers=headers)
    if created.status_code == 402:
        pytest.skip("sap_datasphere requires an enterprise license in this environment")
    assert created.status_code == 200, created.json()
    conn_id = created.json()["id"]

    ok = test_client.get(f"/api/connections/{conn_id}/oauth/authorize", headers=headers)
    assert ok.status_code == 200, ok.json()
    assert ok.json()["authorization_url"].startswith(good + "?")

    async def plant_legacy_config():
        # Pre-validation rows can't be produced through the API any more.
        async with async_session_maker() as db:
            row = await db.scalar(select(Connection).where(Connection.id == conn_id))
            cfg = json.loads(row.config) if isinstance(row.config, str) else dict(row.config or {})
            cfg["authorization_url"] = "javascript:alert(document.cookie)//"
            row.config = cfg if not isinstance(row.config, str) else json.dumps(cfg)
            await db.commit()

    import asyncio
    asyncio.run(plant_legacy_config())

    blocked = test_client.get(f"/api/connections/{conn_id}/oauth/authorize", headers=headers)
    assert blocked.status_code == 400
    assert "authorization_url" not in blocked.json()


# ── On-prem http deployment: the app's own base_url origin ─────────────────

APP_HTTP_BASE = "http://bow.corp:3000"


@pytest.fixture
def http_base_url(monkeypatch):
    from app.settings.config import settings
    monkeypatch.setattr(settings.bow_config, "base_url", APP_HTTP_BASE, raising=False)
    return APP_HTTP_BASE


@pytest.mark.e2e
@pytest.mark.parametrize("path", ["/oauth/callback", "/oauth/callback?x=1"])
def test_own_http_origin_redirect_registers_and_completes_flow(
    path, http_base_url, test_client, create_user, login_user, whoami, create_oauth_client,
):
    # OAuthClientsModal pre-fills `${window.location.origin}/oauth/callback`;
    # on an http on-prem install that default must keep working end to end.
    token, org_id, headers = _admin(create_user, login_user, whoami)
    redirect_uri = f"{http_base_url}{path}"
    client = create_oauth_client(user_token=token, org_id=org_id, redirect_uris=[redirect_uri])
    _complete_auth_code_flow(test_client, headers, client, redirect_uri)


@pytest.mark.e2e
@pytest.mark.parametrize("lookalike", [
    "http://bow.corp.evil.com:3000/oauth/callback",
    "http://evil-bow.corp:3000/oauth/callback",
    "http://bow.corp@evil.com:3000/oauth/callback",
    "http://bow.corp:3000@evil.com/oauth/callback",
    "http://user:pw@bow.corp:3000/oauth/callback",
    "http://bow.corp:3001/oauth/callback",
    "http://bow.corp/oauth/callback",
    "http://bow.corp:3000.evil.com/oauth/callback",
])
def test_own_origin_lookalikes_rejected(lookalike, http_base_url, test_client, create_user, login_user, whoami):
    _, _, headers = _admin(create_user, login_user, whoami)
    resp = test_client.post("/api/oauth/clients", json={"name": "x", "redirect_uris": [lookalike]}, headers=headers)
    assert resp.status_code == 400


@pytest.mark.e2e
def test_own_http_origin_stops_authorizing_when_base_url_changes(
    monkeypatch, test_client, create_user, login_user, whoami, create_oauth_client,
):
    # Saved while the app lived at the http origin; once base_url moves the
    # stored URI must stop authorizing (use-time check).
    from app.settings.config import settings
    monkeypatch.setattr(settings.bow_config, "base_url", APP_HTTP_BASE, raising=False)
    token, org_id, _ = _admin(create_user, login_user, whoami)
    uri = f"{APP_HTTP_BASE}/oauth/callback"
    client = create_oauth_client(user_token=token, org_id=org_id, redirect_uris=[uri])
    monkeypatch.setattr(settings.bow_config, "base_url", "https://bow.example.com", raising=False)
    info = test_client.get(f"/api/oauth/clients/{client['client_id']}/info", params={"redirect_uri": uri})
    assert info.status_code == 400


@pytest.mark.e2e
def test_http_non_loopback_rejected_without_matching_base_url(test_client, create_user, login_user, whoami):
    _, _, headers = _admin(create_user, login_user, whoami)
    resp = test_client.post("/api/oauth/clients", json={"name": "x", "redirect_uris": [f"{APP_HTTP_BASE}/oauth/callback"]},
                            headers=headers)
    assert resp.status_code == 400
