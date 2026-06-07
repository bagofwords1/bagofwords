"""Regression: OAuth token must bind to the CLIENT's org, not the browser's
active org (X-Organization-Id) at consent time.

Scenario (rare but possible): one user belongs to two orgs on the same
deployment. They register an OAuth client under org B. But when they approve
the consent screen, their browser's active org is A, so the consent POST
carries X-Organization-Id: A. Before the fix, the authorization code — and
therefore the access token — was bound to org A, even though the client
belongs to org B, so the MCP client operated against the wrong tenant.
"""

import hashlib
import base64
import secrets

import pytest

from app.settings.config import settings as bow_settings


@pytest.fixture
def _allow_multiple_orgs():
    """Allow a user to belong to more than one org, and allow extra signups —
    needed to construct the rare multi-org / multi-user scenarios."""
    flags = bow_settings.bow_config.features
    saved = (flags.allow_multiple_organizations, flags.allow_uninvited_signups)
    flags.allow_multiple_organizations = True
    flags.allow_uninvited_signups = True
    try:
        yield
    finally:
        flags.allow_multiple_organizations, flags.allow_uninvited_signups = saved


def _pkce_pair():
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _resolve_token_org(access_token):
    """Return the organization_id an OAuth access token resolves to."""
    import asyncio
    from app.settings.database import create_async_session_factory
    from app.services.oauth_server_service import OAuthServerService

    async def _run():
        session_factory = create_async_session_factory()
        async with session_factory() as db:
            result = await OAuthServerService().validate_access_token(db, access_token)
            assert result is not None, "token did not validate"
            _, org = result
            return str(org.id)

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run())
    finally:
        loop.close()


def _run_flow(test_client, client, redirect_uri, consent_org_id):
    """Consent + token exchange. Returns the access token."""
    code_verifier, code_challenge = _pkce_pair()
    authorize_response = test_client.post(
        "/api/oauth/authorize",
        json={
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "state": "s",
            "scope": "mcp",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        },
        headers={
            "Authorization": f"Bearer {consent_org_id['token']}",
            "X-Organization-Id": str(consent_org_id["org_id"]),
        },
    )
    assert authorize_response.status_code == 200, authorize_response.json()
    from urllib.parse import urlparse, parse_qs
    auth_code = parse_qs(urlparse(authorize_response.json()["redirect_url"]).query)["code"][0]

    token_response = test_client.post(
        "/api/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": redirect_uri,
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "code_verifier": code_verifier,
        },
    )
    assert token_response.status_code == 200, token_response.json()
    return token_response.json()["access_token"]


@pytest.mark.e2e
def test_oauth_token_binds_to_client_org_not_active_org(
    _allow_multiple_orgs,
    create_oauth_client,
    create_organization,
    test_client,
    create_user,
    login_user,
    whoami,
):
    """A token minted via org B's client must be scoped to org B, even when the
    consent request's active org (X-Organization-Id) points at org A."""
    user = create_user()
    token = login_user(user["email"], user["password"])

    # Org A is auto-created on registration; org B is created explicitly.
    org_a = whoami(token)["organizations"][0]["id"]
    org_b = create_organization(name="Org B", user_token=token)

    # The client is registered under org B.
    client = create_oauth_client(user_token=token, org_id=org_b, name="MCP for B")
    redirect_uri = "https://claude.ai/api/mcp/auth_callback"

    # The user approves consent while their browser's active org is A.
    access_token = _run_flow(
        test_client, client, redirect_uri,
        consent_org_id={"token": token, "org_id": org_a},
    )

    # The token was minted via org B's client, so it must be scoped to org B.
    bound_org_id = _resolve_token_org(access_token)
    assert bound_org_id == str(org_b), (
        f"OAuth token minted via org B's client ({org_b}) was bound to org "
        f"{bound_org_id}. A token's org must come from the client's org, not "
        f"the active-org header (org_a={org_a}) sent at consent."
    )


@pytest.mark.e2e
def test_oauth_consent_rejected_for_non_member(
    _allow_multiple_orgs,
    create_oauth_client,
    test_client,
    create_user,
    login_user,
    whoami,
):
    """A user who is not a member of the client's org cannot mint a token for it."""
    # Owner of org B registers the client.
    owner = create_user(email="owner_b@test.com")
    owner_token = login_user(owner["email"], owner["password"])
    org_b = whoami(owner_token)["organizations"][0]["id"]
    client = create_oauth_client(user_token=owner_token, org_id=org_b, name="MCP for B")

    # An outsider (member of their own org A only) approves consent for org B's client.
    outsider = create_user(email="outsider_a@test.com")
    outsider_token = login_user(outsider["email"], outsider["password"])

    _, code_challenge = _pkce_pair()
    resp = test_client.post(
        "/api/oauth/authorize",
        json={
            "client_id": client["client_id"],
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "state": "s",
            "scope": "mcp",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        },
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert resp.status_code == 403, resp.json()
