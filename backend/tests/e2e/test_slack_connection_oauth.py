"""E2E: "Sign in with Slack" on a slack_messages connection.

Runs the real authorize + callback routes and the real Slack OAuth branch;
only the two external boundaries are faked — Slack's token endpoint
(``oauth.v2.access``) and the Web API calls the post-sign-in connection test
makes. Pins the Slack-specific parts of the flow:

- scopes go out as ``user_scope`` (``scope`` would mint a bot token, which
  can't search and only sees channels the bot was invited to);
- the stored token is the USER token from ``authed_user``, with no expiry
  (Slack user tokens don't expire without rotation, so no refresh loop);
- DM scopes are always requested (reading DMs is a per-agent choice).
"""
import json
import urllib.parse
from unittest.mock import patch

import httpx
import pytest

from app.data_sources.clients.slack_messages_client import SlackMessagesClient

pytestmark = pytest.mark.e2e


def _make_conn(create_connection, token, org_id, config=None):
    return create_connection(
        name="Slack",
        type="slack_messages",
        config=config or {},
        credentials={"oauth_client_id": "123.456", "oauth_client_secret": "shh"},
        auth_policy="user_required",
        allowed_user_auth_modes=["oauth"],
        user_token=token,
        org_id=org_id,
    )


def _authorize(test_client, conn, token, org_id):
    resp = test_client.get(
        f"/api/connections/{conn['id']}/oauth/authorize",
        headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
    )
    assert resp.status_code == 200, resp.text
    url = resp.json()["authorization_url"]
    return url, urllib.parse.parse_qs(urllib.parse.urlparse(url).query)


class _FakeTokenEndpoint:
    """Stands in for pinned_async_client around Slack's oauth.v2.access."""

    def __init__(self, body: dict):
        self.body = body
        self.posts: list[dict] = []

    def __call__(self, policy):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, data=None, auth=None, headers=None, timeout=None):
        self.posts.append({"url": url, "data": dict(data or {})})
        return httpx.Response(200, content=json.dumps(self.body).encode(),
                              request=httpx.Request("POST", url))


def _fake_slack_api(seen_tokens: list):
    def _call(self, method, **params):
        seen_tokens.append(self.access_token)
        if method == "auth.test":
            return {"ok": True, "url": "https://acme.slack.com/", "team": "Acme", "user": "alex"}
        if method == "users.conversations":
            return {"ok": True, "channels": [{"id": "C0SUPPORT1", "name": "support", "is_channel": True}],
                    "response_metadata": {"next_cursor": ""}}
        raise AssertionError(f"unexpected Slack call {method}")
    return _call


class TestSlackAuthorize:
    def test_authorize_sends_user_scopes_to_slack(
        self, test_client, login_user, create_connection, create_user, whoami
    ):
        user = create_user()
        token = login_user(user["email"], user["password"])
        org_id = whoami(token)["organizations"][0]["id"]
        conn = _make_conn(create_connection, token, org_id)

        url, query = _authorize(test_client, conn, token, org_id)

        assert url.startswith("https://slack.com/oauth/v2/authorize?")
        assert query["client_id"] == ["123.456"]
        assert "scope" not in query  # bot scopes: never requested
        scopes = query["user_scope"][0].split(",")
        for s in ("channels:read", "channels:history", "groups:history", "search:read", "users:read", "im:history"):
            assert s in scopes
        assert "access_type" not in query and "prompt" not in query  # Google-only params
        assert query["redirect_uri"][0].endswith("/api/connections/oauth/callback")


class TestSlackCallback:
    def test_callback_stores_the_user_token_without_expiry(
        self, test_client, login_user, create_connection, create_user, whoami
    ):
        user = create_user()
        token = login_user(user["email"], user["password"])
        org_id = whoami(token)["organizations"][0]["id"]
        conn = _make_conn(create_connection, token, org_id)
        _url, query = _authorize(test_client, conn, token, org_id)

        endpoint = _FakeTokenEndpoint({
            "ok": True,
            "authed_user": {"id": "U1", "access_token": "xoxp-user-token", "token_type": "user",
                            "scope": "search:read"},
        })
        seen: list = []
        with patch("app.services.connection_oauth_service.pinned_async_client", endpoint), \
                patch.object(SlackMessagesClient, "_call", _fake_slack_api(seen)):
            resp = test_client.get(
                f"/api/connections/oauth/callback?code=slack-code&state={query['state'][0]}",
                headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
                follow_redirects=False,
            )

        assert resp.status_code in (302, 307)
        assert "oauth=success" in resp.headers.get("location", ""), resp.headers.get("location")
        post = endpoint.posts[0]
        assert post["url"] == "https://slack.com/api/oauth.v2.access"
        assert post["data"]["code"] == "slack-code" and post["data"]["client_secret"] == "shh"
        # The post-sign-in connection test ran as the user, with the user token.
        assert seen and set(seen) == {"xoxp-user-token"}

        cred = test_client.get(
            f"/api/connections/{conn['id']}/my-credentials",
            headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
        )
        if cred.status_code == 200:
            body = cred.json()
            assert body.get("auth_mode") == "oauth"
            assert body.get("expires_at") in (None, "")

    def test_callback_rejects_a_bot_only_grant(
        self, test_client, login_user, create_connection, create_user, whoami
    ):
        user = create_user()
        token = login_user(user["email"], user["password"])
        org_id = whoami(token)["organizations"][0]["id"]
        conn = _make_conn(create_connection, token, org_id)
        _url, query = _authorize(test_client, conn, token, org_id)

        endpoint = _FakeTokenEndpoint({"ok": True, "access_token": "xoxb-bot", "token_type": "bot"})
        with patch("app.services.connection_oauth_service.pinned_async_client", endpoint):
            resp = test_client.get(
                f"/api/connections/oauth/callback?code=slack-code&state={query['state'][0]}",
                headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
                follow_redirects=False,
            )

        assert resp.status_code in (302, 307)
        location = urllib.parse.unquote_plus(resp.headers.get("location", ""))
        assert "oauth=success" not in location
        assert "User Token Scopes" in location
