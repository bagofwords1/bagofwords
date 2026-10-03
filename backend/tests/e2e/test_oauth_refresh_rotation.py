"""OAuth server: short-lived MCP access tokens kept alive by refresh rotation.

Contract:
  - MCP access tokens live for ``oauth_server.mcp_access_token_ttl_seconds``;
    an expired one gets a 401 that tells the client to refresh.
  - Each refresh retires the presented refresh token and issues a new pair.
  - The retired token presented again inside the grace window (a client retry
    or two refreshes racing) gets a fresh pair; outside it, it is treated as a
    leaked token and every token from that sign-in is revoked.
  - Refreshing never extends a sign-in past ``refresh_session_max_days``.
"""

import base64
import hashlib
import secrets
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from app.services import oauth_server_service
from app.settings.config import settings as bow_settings

REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"


def _pkce_pair():
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class _Clock:
    """Controllable stand-in for the OAuth service's clock."""

    def __init__(self):
        self.now = datetime.utcnow()

    def advance(self, **delta):
        self.now += timedelta(**delta)

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(oauth_server_service, "_utcnow", c)
    return c


@pytest.fixture
def oauth_cfg(monkeypatch):
    cfg = bow_settings.bow_config.oauth_server

    def _set(**values):
        for key, value in values.items():
            monkeypatch.setattr(cfg, key, value)

    return _set


@pytest.fixture
def mcp_session(enable_mcp, create_oauth_client, test_client, create_user, login_user, whoami):
    """A signed-in MCP connection: returns (client, first token response).

    The owner's login token and org ride along on the client dict.
    """
    def _make(client=None):
        """Sign in to a new client, or again to ``client`` (same owner)."""
        if client is None:
            user = create_user()
            token = login_user(user["email"], user["password"])
            org_id = whoami(token)["organizations"][0]["id"]
            enable_mcp(user_token=token, org_id=org_id)
            client = create_oauth_client(user_token=token, org_id=org_id, scopes="mcp")
            client["_owner"] = {"token": token, "org_id": org_id}
        token = client["_owner"]["token"]
        org_id = client["_owner"]["org_id"]

        verifier, challenge = _pkce_pair()
        consent = test_client.post(
            "/api/oauth/authorize",
            json={
                "client_id": client["client_id"],
                "redirect_uri": REDIRECT_URI,
                "scope": "mcp",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
            headers={"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)},
        )
        assert consent.status_code == 200, consent.json()
        code = parse_qs(urlparse(consent.json()["redirect_url"]).query)["code"][0]
        issued = test_client.post(
            "/api/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "code_verifier": verifier,
            },
        )
        assert issued.status_code == 200, issued.json()
        return client, issued.json()

    return _make


def _refresh(test_client, client, refresh_token):
    return test_client.post(
        "/api/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
        },
    )


def _mcp_status(test_client, access_token):
    return test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": f"Bearer {access_token}"},
    )


@pytest.mark.e2e
@pytest.mark.parametrize("ttl_seconds", [600, 3600])
def test_mcp_access_token_lifetime_follows_config(oauth_cfg, mcp_session, ttl_seconds):
    oauth_cfg(mcp_access_token_ttl_seconds=ttl_seconds)
    _, issued = mcp_session()
    assert issued["expires_in"] == ttl_seconds


@pytest.mark.e2e
def test_expired_mcp_token_is_rejected_and_refresh_restores_access(
    clock, oauth_cfg, mcp_session, test_client,
):
    oauth_cfg(mcp_access_token_ttl_seconds=900)
    client, issued = mcp_session()
    assert _mcp_status(test_client, issued["access_token"]).status_code == 200

    clock.advance(seconds=901)
    expired = _mcp_status(test_client, issued["access_token"])
    assert expired.status_code == 401
    # The client must be told to refresh, not to start sign-in over.
    assert 'error="invalid_token"' in expired.headers["www-authenticate"]

    refreshed = _refresh(test_client, client, issued["refresh_token"])
    assert refreshed.status_code == 200, refreshed.json()
    assert _mcp_status(test_client, refreshed.json()["access_token"]).status_code == 200


@pytest.mark.e2e
def test_rotated_refresh_token_retried_within_grace_gets_a_working_pair(
    clock, oauth_cfg, mcp_session, test_client,
):
    oauth_cfg(refresh_reuse_grace_seconds=60)
    client, issued = mcp_session()

    first = _refresh(test_client, client, issued["refresh_token"])
    assert first.status_code == 200, first.json()

    clock.advance(seconds=30)
    retry = _refresh(test_client, client, issued["refresh_token"])
    assert retry.status_code == 200, retry.json()
    assert retry.json()["refresh_token"] != first.json()["refresh_token"]

    # Neither response's credentials were thrown away by the retry.
    for pair in (first.json(), retry.json()):
        assert _mcp_status(test_client, pair["access_token"]).status_code == 200
        assert _refresh(test_client, client, pair["refresh_token"]).status_code == 200


@pytest.mark.e2e
@pytest.mark.parametrize("grace_seconds,replay_after", [(0, 1), (60, 61), (300, 3600)])
def test_rotated_refresh_token_replayed_after_grace_revokes_the_whole_sign_in(
    clock, oauth_cfg, mcp_session, test_client, grace_seconds, replay_after,
):
    oauth_cfg(refresh_reuse_grace_seconds=grace_seconds)
    client, issued = mcp_session()
    current = _refresh(test_client, client, issued["refresh_token"]).json()

    clock.advance(seconds=replay_after)
    replay = _refresh(test_client, client, issued["refresh_token"])
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"

    # The legitimate holder's current credentials die with the leaked one.
    assert _mcp_status(test_client, current["access_token"]).status_code == 401
    assert _refresh(test_client, client, current["refresh_token"]).status_code == 400


@pytest.mark.e2e
def test_replay_does_not_touch_other_sign_ins_of_the_same_client(
    clock, oauth_cfg, mcp_session, test_client,
):
    oauth_cfg(refresh_reuse_grace_seconds=0)
    client, victim = mcp_session()
    _, bystander = mcp_session(client)

    _refresh(test_client, client, victim["refresh_token"])
    clock.advance(seconds=5)
    assert _refresh(test_client, client, victim["refresh_token"]).status_code == 400

    assert _mcp_status(test_client, bystander["access_token"]).status_code == 200


@pytest.mark.e2e
def test_refresh_cannot_extend_a_sign_in_past_the_session_cap(
    clock, oauth_cfg, mcp_session, test_client,
):
    oauth_cfg(refresh_token_ttl_days=30, refresh_session_max_days=45)
    client, issued = mcp_session()

    clock.advance(days=25)
    second = _refresh(test_client, client, issued["refresh_token"])
    assert second.status_code == 200, second.json()

    # Within the sliding 30 days of the last refresh, but past the 45-day cap.
    clock.advance(days=21)
    capped = _refresh(test_client, client, second.json()["refresh_token"])
    assert capped.status_code == 400


@pytest.mark.e2e
def test_unused_refresh_token_expires_after_its_sliding_window(
    clock, oauth_cfg, mcp_session, test_client,
):
    oauth_cfg(refresh_token_ttl_days=30, refresh_session_max_days=365)
    client, issued = mcp_session()

    clock.advance(days=31)
    assert _refresh(test_client, client, issued["refresh_token"]).status_code == 400


@pytest.mark.e2e
def test_active_token_count_counts_connections_not_unexpired_access_tokens(
    clock, oauth_cfg, mcp_session, list_oauth_clients,
):
    """The admin list must keep showing a connection whose short access token
    lapsed but whose refresh token still works."""
    oauth_cfg(mcp_access_token_ttl_seconds=600)
    client, _ = mcp_session()
    clock.advance(hours=2)

    owner = client["_owner"]
    listed = list_oauth_clients(user_token=owner["token"], org_id=owner["org_id"])
    match = next(c for c in listed if c["client_id"] == client["client_id"])
    assert match["active_token_count"] == 1


@pytest.mark.e2e
@pytest.mark.parametrize("racers", [2, 4])
def test_concurrent_refreshes_of_one_token_all_succeed(oauth_cfg, mcp_session, test_client, racers):
    """Connectors fire parallel requests; when several hit an expired access
    token at once, each refreshes with the same refresh token. Every racer
    must get a working pair (no 500, no sign-out). Runs each refresh in its
    own DB session, as separate server workers would."""
    import asyncio

    from app.services.oauth_server_service import OAuthServerService
    from app.settings.database import create_async_session_factory

    oauth_cfg(refresh_reuse_grace_seconds=60)
    client, issued = mcp_session()
    session_factory = create_async_session_factory()

    async def _one():
        async with session_factory() as db:
            return await OAuthServerService().refresh_access_token(
                db, issued["refresh_token"], client["client_id"], client["client_secret"],
            )

    async def _race():
        return await asyncio.gather(*[_one() for _ in range(racers)])

    loop = asyncio.new_event_loop()
    try:
        pairs = loop.run_until_complete(_race())
    finally:
        loop.close()

    assert all(pair is not None for pair in pairs)
    assert len({pair["access_token"] for pair in pairs}) == racers
    for pair in pairs:
        assert _mcp_status(test_client, pair["access_token"]).status_code == 200
