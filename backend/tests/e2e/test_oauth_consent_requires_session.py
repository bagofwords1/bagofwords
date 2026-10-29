"""e2e: OAuth consent can only be approved by an interactive login session.

`POST /api/oauth/authorize` turns the caller's identity into an authorization
code for *any* registered client. If a delegated credential (an OAuth access
token minted for some client, or an API key) could call it, that credential
could silently grant itself — or another client — new scopes and a fresh
refresh-token chain without the user ever seeing a consent screen.
"""
import base64
import hashlib
import uuid
from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = pytest.mark.e2e

REDIRECT = "http://localhost:4173/callback"
VERIFIER = "c" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


def _account(create_user, login_user, whoami):
    email = f"consent_{uuid.uuid4().hex[:10]}@test.com"
    create_user(email=email, password="Test1234!")
    token = login_user(email, "Test1234!")
    org_id = whoami(token)["organizations"][0]["id"]
    return token, org_id


def _approve(test_client, headers, client_id, scope):
    return test_client.post("/api/oauth/authorize", headers=headers, json={
        "client_id": client_id, "redirect_uri": REDIRECT,
        "scope": scope, "code_challenge": CHALLENGE,
    })


def _oauth_access_token(test_client, login_token, client_id, scope):
    approved = _approve(test_client, {"Authorization": f"Bearer {login_token}"}, client_id, scope)
    assert approved.status_code == 200, approved.text
    code = parse_qs(urlparse(approved.json()["redirect_url"]).query)["code"][0]
    tokens = test_client.post("/api/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "code_verifier": VERIFIER, "redirect_uri": REDIRECT,
    })
    assert tokens.status_code == 200, tokens.text
    return tokens.json()["access_token"]


@pytest.mark.parametrize("target", ["other_client", "same_client"])
def test_oauth_access_token_cannot_approve_consent(
    test_client, create_user, login_user, whoami, create_oauth_client, target
):
    login_token, org_id = _account(create_user, login_user, whoami)
    issuer = create_oauth_client(login_token, org_id, scopes="app", redirect_uris=[REDIRECT])
    other = create_oauth_client(login_token, org_id, name="Other", scopes="app mcp",
                                redirect_uris=[REDIRECT])
    access = _oauth_access_token(test_client, login_token, issuer["client_id"], "app")
    # The token is genuinely valid on the app API — only consent is off-limits.
    assert test_client.get("/api/users/whoami",
                           headers={"Authorization": f"Bearer {access}"}).status_code == 200

    victim = other if target == "other_client" else issuer
    response = _approve(test_client, {"Authorization": f"Bearer {access}"},
                        victim["client_id"], "app mcp" if victim is other else "app")
    assert response.status_code == 401
    assert "redirect_url" not in response.text


def test_api_key_cannot_approve_consent(
    test_client, create_user, login_user, whoami, create_oauth_client, create_api_key
):
    login_token, org_id = _account(create_user, login_user, whoami)
    client = create_oauth_client(login_token, org_id, scopes="app mcp", redirect_uris=[REDIRECT])
    key = create_api_key(login_token, org_id)["key"]

    for headers in ({"X-API-Key": key}, {"Authorization": f"Bearer {key}"}):
        response = _approve(test_client, headers, client["client_id"], "app mcp")
        assert response.status_code == 401
        assert "redirect_url" not in response.text


def test_login_session_still_approves_consent(
    test_client, create_user, login_user, whoami, create_oauth_client
):
    login_token, org_id = _account(create_user, login_user, whoami)
    client = create_oauth_client(login_token, org_id, scopes="app mcp", redirect_uris=[REDIRECT])
    access = _oauth_access_token(test_client, login_token, client["client_id"], "app mcp")
    assert access.startswith("bow_oauth_")
