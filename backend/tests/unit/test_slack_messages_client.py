"""Contract tests for the Slack messages connector (``slack_messages``).

Every Slack Web API call goes through ``httpx.MockTransport``, so these run
without a workspace or token. They pin the behaviour the agent relies on:
membership-only conversation listing, cursor pagination, 429 + Retry-After,
Slack's ``ok: false`` errors, message links, thread reads, and the optional
``allowed_channels`` scope (which only ever narrows what the user can see).
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.data_sources.clients.base import Capability
from app.data_sources.clients.slack_messages_client import (
    SlackApiError,
    SlackMessagesClient,
    parse_allowed_channels,
    parse_slack_id,
)

WORKSPACE = "https://acme.slack.com/"
ALL_SCOPES = ("channels:read,groups:read,channels:history,groups:history,search:read,"
              "users:read,im:read,im:history,mpim:read,mpim:history")

CHANNELS = [
    {"id": "C0SUPPORT1", "name": "support", "is_channel": True, "is_private": False},
    {"id": "C0SALES001", "name": "sales", "is_channel": True, "is_private": False},
    {"id": "G0PRIVATE1", "name": "leadership", "is_group": True, "is_private": True},
    {"id": "D0DIRECT01", "is_im": True, "user": "U0DANA0001"},
    {"id": "C0MPIM0001", "name": "mpdm-dana--omer--noa-1", "is_mpim": True},
]
USERS = {"U0DANA0001": "Dana", "U0OMER0001": "Omer"}


class FakeSlack:
    """A tiny Slack Web API: routes ``/api/<method>`` to handlers, records calls."""

    def __init__(self, scopes: str = ALL_SCOPES):
        self.calls: list[tuple[str, dict]] = []
        self.scopes = scopes
        self.routes: dict = {
            "auth.test": lambda q: {"ok": True, "url": WORKSPACE, "team": "Acme", "user": "alex",
                                    "team_id": "T1", "user_id": "U0ME000001"},
            "users.info": lambda q: (
                {"ok": True, "user": {"id": q["user"], "profile": {"display_name": USERS[q["user"]]}}}
                if q["user"] in USERS else {"ok": False, "error": "user_not_found"}
            ),
            "users.conversations": self._conversations,
        }

    def _conversations(self, q):
        types = q["types"].split(",")
        rows = [c for c in CHANNELS if (
            ("public_channel" in types and c.get("is_channel") and not c.get("is_private"))
            or ("private_channel" in types and c.get("is_group"))
            or ("im" in types and c.get("is_im"))
            or ("mpim" in types and c.get("is_mpim"))
        )]
        # Two pages, to exercise the cursor walk.
        if not q.get("cursor"):
            return {"ok": True, "channels": rows[:2], "response_metadata": {"next_cursor": "page2"}}
        return {"ok": True, "channels": rows[2:], "response_metadata": {"next_cursor": ""}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer xoxp-user"
        # Slack rejects GET requests with a body: arguments ride the query only.
        assert request.method == "GET" and not request.content
        method = urlparse(str(request.url)).path.rsplit("/", 1)[-1]
        q = {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}
        self.calls.append((method, q))
        out = self.routes[method](q)
        if isinstance(out, httpx.Response):
            return out
        return httpx.Response(200, json=out, headers={"x-oauth-scopes": self.scopes})

    def count(self, method: str) -> int:
        return sum(1 for m, _ in self.calls if m == method)


def _client(fake: FakeSlack, **kwargs) -> SlackMessagesClient:
    kwargs.setdefault("sleep", lambda s: fake.calls.append(("sleep", {"s": s})))
    return SlackMessagesClient(access_token="xoxp-user", transport=httpx.MockTransport(fake.handler), **kwargs)


def _msg(ts: str, user: str = "U0DANA0001", text: str = "hello", **extra) -> dict:
    return {"type": "message", "ts": ts, "user": user, "text": text, **extra}


# ------------------------------------------------------------------ basics


def test_declares_the_chat_capabilities_only():
    assert SlackMessagesClient.capabilities == {
        Capability.LIST_CHAT_MESSAGES, Capability.READ_CHAT_THREAD, Capability.SEARCH_CHAT_MESSAGES,
    }


def test_ids_round_trip_and_reject_foreign_ids():
    assert parse_slack_id("slack:C0SUPPORT1/1700000000.000100") == {
        "channel_id": "C0SUPPORT1", "ts": "1700000000.000100"}
    assert parse_slack_id("slack:C0SUPPORT1") == {"channel_id": "C0SUPPORT1", "ts": None}
    for bad in ("channel:T/C/1", "slack:not-an-id", "slack:C0SUPPORT1/abc"):
        with pytest.raises(ValueError):
            parse_slack_id(bad)


def test_allowed_channels_parse_names_and_ids():
    assert parse_allowed_channels("#Support, escalations  C0SALES001") == ["support", "escalations", "C0SALES001"]
    assert parse_allowed_channels(["#a", "a"]) == ["a"]
    assert parse_allowed_channels(None) == []


# ----------------------------------------------------------- conversations


def test_lists_member_conversations_across_pages_with_readable_names():
    fake = FakeSlack()
    rows = _client(fake).list_conversations()
    # users.conversations (membership only) — never conversations.list.
    assert fake.count("users.conversations") == 2 and fake.count("conversations.list") == 0
    first_q = fake.calls[0][1]
    assert first_q["types"] == "public_channel,private_channel,im,mpim"
    assert first_q["exclude_archived"] == "true"
    by_id = {r["channel_id"]: r for r in rows}
    assert by_id["C0SUPPORT1"]["name"] == "#support" and by_id["C0SUPPORT1"]["kind"] == "channel"
    assert by_id["G0PRIVATE1"]["kind"] == "private channel"
    assert by_id["D0DIRECT01"]["name"] == "DM with Dana"
    assert by_id["C0MPIM0001"]["name"] == "Group DM: dana, omer, noa"
    assert by_id["C0SUPPORT1"]["web_url"] == f"{WORKSPACE}archives/C0SUPPORT1"


def test_dms_can_be_turned_off():
    fake = FakeSlack()
    rows = _client(fake, include_direct_messages=False).list_conversations()
    assert fake.calls[0][1]["types"] == "public_channel,private_channel"
    assert {r["kind"] for r in rows} == {"channel", "private channel"}


def test_allowed_channels_narrow_channels_and_dms_follow_their_own_switch():
    fake = FakeSlack()
    with_dms = _client(fake, allowed_channels="#support, G0PRIVATE1, #not-a-member").list_conversations()
    assert fake.calls[0][1]["types"] == "public_channel,private_channel,im,mpim"
    assert [r["channel_id"] for r in with_dms] == ["C0SUPPORT1", "G0PRIVATE1", "D0DIRECT01", "C0MPIM0001"]

    fake = FakeSlack()
    client = _client(fake, allowed_channels="#support, G0PRIVATE1, #not-a-member", include_direct_messages=False)
    rows = client.list_conversations()
    assert fake.calls[0][1]["types"] == "public_channel,private_channel"
    assert [r["channel_id"] for r in rows] == ["C0SUPPORT1", "G0PRIVATE1"]
    # A visible-to-the-user channel outside the list cannot be resolved or read.
    with pytest.raises(ValueError, match="this agent reads"):
        client.resolve_conversation("C0SALES001")
    with pytest.raises(ValueError, match="this agent reads"):
        client.read_file("slack:C0SALES001/1700000000.000100")
    with pytest.raises(ValueError, match="not found"):
        client.resolve_conversation("#sales")


def test_resolves_conversations_by_name_or_id():
    client = _client(FakeSlack())
    assert client.resolve_conversation("#support")["channel_id"] == "C0SUPPORT1"
    assert client.resolve_conversation("support")["channel_id"] == "C0SUPPORT1"
    assert client.resolve_conversation("slack:G0PRIVATE1")["channel_id"] == "G0PRIVATE1"
    assert client.resolve_conversation("DM with Dana")["channel_id"] == "D0DIRECT01"


# ---------------------------------------------------------------- HTTP errors


def test_429_waits_retry_after_then_succeeds():
    fake = FakeSlack()
    hits = {"n": 0}

    def flaky(q):
        hits["n"] += 1
        if hits["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"ok": False, "error": "ratelimited"})
        return {"ok": True, "url": WORKSPACE, "team": "Acme", "user": "alex"}

    fake.routes["auth.test"] = flaky
    assert _client(fake)._whoami()["team"] == "Acme"
    assert ("sleep", {"s": 2.0}) in fake.calls


def test_429_gives_up_after_bounded_retries_with_a_clear_error():
    fake = FakeSlack()
    fake.routes["auth.test"] = lambda q: httpx.Response(429, headers={"Retry-After": "120"}, json={"ok": False})
    with pytest.raises(SlackApiError) as exc:
        _client(fake)._whoami()
    assert exc.value.error == "ratelimited" and "rate-limiting" in str(exc.value)
    # Retry-After is honoured but capped, and retries are bounded.
    sleeps = [c[1]["s"] for c in fake.calls if c[0] == "sleep"]
    assert sleeps == [30.0, 30.0, 30.0]


def test_ok_false_not_in_channel_is_a_friendly_error():
    fake = FakeSlack()
    fake.routes["conversations.history"] = lambda q: {"ok": False, "error": "not_in_channel"}
    with pytest.raises(SlackApiError, match="not a member"):
        _client(fake).list_messages(conversation="#support")


def test_revoked_token_asks_the_user_to_sign_in_again():
    fake = FakeSlack()
    fake.routes["auth.test"] = lambda q: {"ok": False, "error": "token_revoked"}
    result = _client(fake).test_connection()
    assert result["success"] is False and "Sign in with Slack again" in result["message"]


def test_missing_scope_names_what_is_needed():
    fake = FakeSlack()
    fake.routes["search.messages"] = lambda q: {"ok": False, "error": "missing_scope", "needed": "search:read"}
    with pytest.raises(SlackApiError, match="search:read"):
        _client(fake).search_messages("churn")


# ------------------------------------------------------------------ listing


def test_list_messages_pages_history_and_builds_links():
    fake = FakeSlack()
    page1 = [_msg("1700000300.000100", text="third", reply_count=2, thread_ts="1700000300.000100"),
             {"type": "message", "subtype": "channel_join", "ts": "1700000250.000000", "user": "U0OMER0001"},
             _msg("1700000200.000100", user="U0OMER0001", text="second <@U0DANA0001> see <https://x.io|doc>")]
    page2 = [_msg("1700000100.000100", text="first")]

    def history(q):
        assert q["channel"] == "C0SUPPORT1"
        if not q.get("cursor"):
            return {"ok": True, "messages": page1, "has_more": True, "response_metadata": {"next_cursor": "h2"}}
        return {"ok": True, "messages": page2, "has_more": False}

    fake.routes["conversations.history"] = history
    client = _client(fake)
    out = client.list_messages(conversation="#support", max_results=2)
    items = out["items"]
    assert [i["preview"] for i in items] == ["third", "second @Dana see doc (https://x.io)"]
    assert items[0]["id"] == "slack:C0SUPPORT1/1700000300.000100"
    assert items[0]["reply_count"] == 2 and items[0]["is_reply"] is False
    assert items[0]["web_url"] == f"{WORKSPACE}archives/C0SUPPORT1/p1700000300000100"
    assert items[1]["sender"] == "Omer"
    assert out["folder"]["name"] == "#support" and out["next_cursor"]

    rest = client.list_messages(conversation="#support", max_results=2, cursor=out["next_cursor"])
    assert [i["preview"] for i in rest["items"]] == ["first"] and rest["next_cursor"] is None


def test_list_messages_passes_the_window_and_filters_sender():
    fake = FakeSlack()
    seen = {}

    def history(q):
        seen.update(q)
        return {"ok": True, "messages": [_msg("1700000200.000100", user="U0OMER0001"), _msg("1700000100.000100")],
                "has_more": False}

    fake.routes["conversations.history"] = history
    out = _client(fake).list_messages(conversation="support", from_user="dana",
                                      sent_after="2023-11-14T00:00:00Z", sent_before="2023-11-16")
    assert float(seen["oldest"]) == pytest.approx(1699920000.0)
    assert float(seen["latest"]) == pytest.approx(1700092800.0)
    assert [i["sender"] for i in out["items"]] == ["Dana"]


def test_list_without_conversation_fans_out_over_allowed_channels_only():
    fake = FakeSlack()
    fake.routes["conversations.history"] = lambda q: {
        "ok": True, "has_more": False,
        "messages": [_msg("1700000100.000100" if q["channel"] == "C0SUPPORT1" else "1700000200.000100",
                          text=q["channel"])]}
    out = _client(fake, allowed_channels="#support", include_direct_messages=False).list_messages()
    channels = {q["channel"] for m, q in fake.calls if m == "conversations.history"}
    assert channels == {"C0SUPPORT1"}
    assert [i["preview"] for i in out["items"]] == ["C0SUPPORT1"]


# ------------------------------------------------------------------- search


def _match(cid: str, ts: str, text: str = "churn is up", thread_ts: str | None = None, name: str = "support"):
    link = f"{WORKSPACE}archives/{cid}/p{ts.replace('.', '')}"
    if thread_ts:
        link += f"?thread_ts={thread_ts}&cid={cid}"
    return {"channel": {"id": cid, "name": name}, "ts": ts, "text": text, "user": "U0DANA0001",
            "username": "dana", "permalink": link}


def test_search_uses_slack_permalinks_and_marks_thread_replies():
    fake = FakeSlack()
    fake.routes["search.messages"] = lambda q: {"ok": True, "messages": {
        "matches": [_match("C0SUPPORT1", "1700000200.000100", thread_ts="1700000100.000100"),
                    _match("C0SALES001", "1700000150.000100", name="sales")],
        "paging": {"page": 1, "pages": 1}}}
    out = _client(fake).search_messages("churn")
    q = next(q for m, q in fake.calls if m == "search.messages")
    assert q["query"] == "churn" and q["count"] == "100"
    first = out["items"][0]
    assert first["is_reply"] is True
    assert first["web_url"].endswith("?thread_ts=1700000100.000100&cid=C0SUPPORT1")
    assert first["folder"] == "#support" and out["next_cursor"] is None


def test_search_drops_matches_outside_the_allowed_channels():
    fake = FakeSlack()
    fake.routes["search.messages"] = lambda q: {"ok": True, "messages": {
        "matches": [_match("C0SALES001", "1700000300.000100", name="sales"),
                    _match("D0DIRECT01", "1700000250.000100", name="U0DANA0001"),
                    _match("C0SUPPORT1", "1700000200.000100")],
        "paging": {"page": 1, "pages": 1}}}
    out = _client(fake, allowed_channels="support", include_direct_messages=False).search_messages("churn")
    q = next(q for m, q in fake.calls if m == "search.messages")
    # One allowed channel: Slack scopes the search itself.
    assert q["query"] == "churn in:#support"
    assert [i["conversation_id"] for i in out["items"]] == ["slack:C0SUPPORT1"]


def test_search_pages_with_a_resumable_cursor():
    fake = FakeSlack()

    def search(q):
        page = int(q["page"])
        return {"ok": True, "messages": {
            "matches": [_match("C0SUPPORT1", f"17000001{page}{i}.000100") for i in range(3)],
            "paging": {"page": page, "pages": 2}}}

    fake.routes["search.messages"] = search
    client = _client(fake)
    first = client.search_messages("churn", max_results=4)
    assert len(first["items"]) == 4 and first["next_cursor"]
    rest = client.search_messages("churn", max_results=4, cursor=first["next_cursor"])
    assert len(rest["items"]) == 2 and rest["next_cursor"] is None
    ids = [i["id"] for i in first["items"] + rest["items"]]
    assert len(set(ids)) == 6


def test_search_filters_the_exact_window_and_widens_the_day_bounds():
    fake = FakeSlack()
    fake.routes["search.messages"] = lambda q: {"ok": True, "messages": {
        "matches": [_match("C0SUPPORT1", "1700000000.000100"),   # 2023-11-14 22:13 UTC
                    _match("C0SUPPORT1", "1699900000.000100")],  # 2023-11-13 18:26 UTC
        "paging": {"pages": 1}}}
    out = _client(fake).search_messages("churn", sent_after="2023-11-14T12:00:00Z")
    q = next(q for m, q in fake.calls if m == "search.messages")
    assert q["query"] == "churn after:2023-11-13"
    assert [i["id"] for i in out["items"]] == ["slack:C0SUPPORT1/1700000000.000100"]


def test_search_requires_a_query():
    with pytest.raises(ValueError, match="non-empty"):
        _client(FakeSlack()).search_messages("  ")


# --------------------------------------------------------------------- read


def test_reading_a_reply_returns_the_whole_thread():
    fake = FakeSlack()
    root, reply = "1700000100.000100", "1700000200.000100"

    def replies(q):
        if q["ts"] == reply:
            return {"ok": True, "messages": [_msg(reply, user="U0OMER0001", text="fixed", thread_ts=root)]}
        return {"ok": True, "has_more": False, "messages": [
            _msg(root, text="checkout is down", thread_ts=root, reply_count=1),
            _msg(reply, user="U0OMER0001", text="fixed", thread_ts=root)]}

    fake.routes["conversations.replies"] = replies
    text = _client(fake).read_file(f"slack:C0SUPPORT1/{reply}")
    lines = text.splitlines()
    assert lines[0] == "Slack channel: #support"
    assert "1 reply" in lines[1]
    assert lines[2] == f"Link: {WORKSPACE}archives/C0SUPPORT1/p1700000100000100"
    assert "Dana: checkout is down" in text and "↳ ▶ [" in text and "Omer: fixed" in text


def test_reading_an_unthreaded_message_shows_its_neighbours():
    fake = FakeSlack()
    ts = "1700000200.000100"
    fake.routes["conversations.replies"] = lambda q: {"ok": True, "messages": [_msg(ts, text="target")]}
    fake.routes["conversations.history"] = lambda q: {"ok": True, "messages": (
        [_msg("1700000100.000100", text="before")] if q.get("latest") else [_msg("1700000300.000100", text="after")])}
    text = _client(fake).read_file(f"slack:C0SUPPORT1/{ts}")
    body = text.split("\n\n", 1)[1].splitlines()
    assert [l.split(": ", 1)[1] for l in body] == ["before", "target", "after"]
    assert body[1].startswith("▶ ")


# --------------------------------------------------------------- test button


def test_test_connection_without_a_user_token_saves_the_app():
    result = SlackMessagesClient().test_connection()
    assert result["success"] is True
    assert "signs in with Slack" in result["message"]


def test_test_connection_reports_the_workspace_and_user():
    result = _client(FakeSlack()).test_connection()
    assert result["success"] is True
    assert result["message"] == "Connected as alex on Acme · 5 conversation(s)"


def test_test_connection_flags_missing_user_scopes():
    fake = FakeSlack(scopes="channels:read,channels:history")
    result = _client(fake, include_direct_messages=False).test_connection()
    assert result["success"] is False
    assert "groups:history" in result["message"] and "search:read" in result["message"]
    assert "im:history" not in result["message"]


# -------------------------------------------------------------------- OAuth


def _conn(config=None, creds=None):
    return SimpleNamespace(
        id="conn-1", type="slack_messages", config=config or {},
        decrypt_credentials=lambda: creds if creds is not None else {
            "oauth_client_id": "123.456", "oauth_client_secret": "shh"},
    )


def test_oauth_params_request_every_read_user_scope():
    from app.services.connection_oauth_service import get_oauth_params

    p = get_oauth_params(_conn())
    assert p["provider_name"] == "slack"
    assert p["authorize_url"] == "https://slack.com/oauth/v2/authorize"
    assert p["token_url"] == "https://slack.com/api/oauth.v2.access"
    assert "search:read" in p["scopes"] and "im:history" in p["scopes"]
    assert " " not in p["scopes"]  # Slack takes a comma-separated list
    # DM scopes are always requested: reading DMs is a per-agent choice made
    # after sign-in, on the same token.
    assert "im:history" in get_oauth_params(_conn({"include_direct_messages": False}))["scopes"]
    with pytest.raises(ValueError, match="Client ID"):
        get_oauth_params(_conn(creds={}))


def test_token_response_uses_the_user_token_and_never_expires():
    from app.services.connection_oauth_service import normalize_slack_token_response

    tokens = normalize_slack_token_response({
        "ok": True, "access_token": "xoxb-bot", "token_type": "bot",
        "authed_user": {"id": "U1", "scope": "search:read", "access_token": "xoxp-user", "token_type": "user"},
    })
    assert tokens["access_token"] == "xoxp-user"
    assert tokens["expires_at"] is None and tokens["refresh_token"] is None

    rotated = normalize_slack_token_response({"ok": True, "authed_user": {
        "access_token": "xoxe.xoxp-1", "refresh_token": "xoxe-1", "expires_in": 43200}})
    assert rotated["refresh_token"] == "xoxe-1" and rotated["expires_at"]

    with pytest.raises(ValueError, match="invalid_code"):
        normalize_slack_token_response({"ok": False, "error": "invalid_code"})
    with pytest.raises(ValueError, match="User Token Scopes"):
        normalize_slack_token_response({"ok": True, "access_token": "xoxb-bot"})


@pytest.mark.asyncio
async def test_code_exchange_posts_to_slack_and_returns_the_user_token(monkeypatch):
    from app.services import connection_oauth_service as svc

    sent = {}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None, auth=None, headers=None, timeout=None):
            sent.update(url=url, data=data)
            return httpx.Response(200, content=json.dumps({
                "ok": True, "authed_user": {"access_token": "xoxp-user"}}).encode(),
                request=httpx.Request("POST", url))

    monkeypatch.setattr(svc, "pinned_async_client", lambda policy: _Client())
    tokens = await svc.exchange_code_for_tokens(svc.get_oauth_params(_conn()), "the-code", "https://bow/cb", "verifier")
    assert sent["url"] == "https://slack.com/api/oauth.v2.access"
    assert sent["data"]["client_secret"] == "shh" and sent["data"]["code_verifier"] == "verifier"
    assert tokens == {"access_token": "xoxp-user", "refresh_token": None, "expires_at": None, "token_type": "Bearer"}


# ----------------------------------------------------------------- registry


def test_registry_entry_is_a_dev_only_per_user_chat_source():
    from app.schemas.data_source_registry import get_entry, resolve_client_class

    entry = get_entry("slack_messages")
    assert resolve_client_class("slack_messages") is SlackMessagesClient
    assert entry.dev_only is True and entry.catalog_ownership == "per_user"
    assert entry.credentials_auth.by_auth["oauth"].scopes == ["user"]
    assert set(SlackMessagesClient.capabilities) <= {
        Capability.LIST_CHAT_MESSAGES, Capability.READ_CHAT_THREAD, Capability.SEARCH_CHAT_MESSAGES}



def test_searching_a_channel_name_hints_to_read_the_channel_instead():
    fake = FakeSlack()
    fake.routes["search.messages"] = lambda q: {"ok": True, "messages": {"matches": [], "paging": {"pages": 0}}}
    out = _client(fake).search_messages("support")
    assert out["items"] == []
    assert "name of the conversation #support" in out["hint"]
    assert "list_chat_messages" in out["hint"]
    # An ordinary empty search carries no hint.
    assert "hint" not in _client(fake).search_messages("quarterly churn")


def test_dm_partner_names_are_looked_up_once_each():
    fake = FakeSlack()
    _client(fake).list_conversations()
    looked_up = [q["user"] for m, q in fake.calls if m == "users.info"]
    assert sorted(looked_up) == sorted(set(looked_up))


# ----------------------------------------------------- thread replies (Teams parity)


def _threaded_channel(fake: FakeSlack, *, replies_in_window=True):
    root, reply1, reply2 = "1700000300.000100", "1700000400.000100", "1700000500.000100"
    fake.routes["conversations.history"] = lambda q: {"ok": True, "has_more": False, "messages": [
        # A reply also sent to the channel appears in history as well.
        _msg(reply2, user="U0OMER0001", text="fixed, also posted to channel", thread_ts=root,
             subtype="thread_broadcast"),
        _msg(root, text="3 connections are down", thread_ts=root, reply_count=2),
        _msg("1700000200.000100", text="unrelated earlier post"),
    ]}

    def replies(q):
        assert q["ts"] == root and q["channel"] == "C0SUPPORT1"
        return {"ok": True, "has_more": False, "messages": [
            _msg(root, text="3 connections are down", thread_ts=root, reply_count=2),
            _msg(reply1, user="U0OMER0001", text="checking IAM", thread_ts=root),
            _msg(reply2, user="U0OMER0001", text="fixed, also posted to channel", thread_ts=root),
        ]}

    fake.routes["conversations.replies"] = replies
    return root, reply1, reply2


def test_listing_a_channel_includes_each_threads_replies_like_teams():
    fake = FakeSlack()
    root, reply1, reply2 = _threaded_channel(fake)
    items = _client(fake).list_messages(conversation="#support")["items"]
    assert [i["preview"] for i in items] == [
        "3 connections are down", "checking IAM", "fixed, also posted to channel", "unrelated earlier post"]
    assert [i["is_reply"] for i in items] == [False, True, True, False]
    assert items[0]["reply_count"] == 2
    assert items[1]["web_url"].endswith(f"?thread_ts={root}&cid=C0SUPPORT1")
    # One replies call for the one threaded post; the broadcast copy isn't listed twice.
    assert fake.count("conversations.replies") == 1


def test_replies_respect_the_window_and_sender_filter():
    fake = FakeSlack()
    _threaded_channel(fake)
    # reply1 is 22:20:00 UTC, reply2 22:21:40 UTC — the window keeps only reply1.
    items = _client(fake).list_messages(conversation="#support", sent_before="2023-11-14T22:21:00Z")["items"]
    assert "checking IAM" in [i["preview"] for i in items]
    assert "fixed, also posted to channel" not in [i["preview"] for i in items]
    omer = _client(fake).list_messages(conversation="#support", from_user="omer")["items"]
    assert {i["sender"] for i in omer} == {"Omer"} and len(omer) == 2


def test_a_thread_is_never_split_across_pages():
    fake = FakeSlack()
    _threaded_channel(fake)
    client = _client(fake)
    first = client.list_messages(conversation="#support", max_results=2)
    # Root + 2 replies don't fit in 2 rows, but a page never starts empty: the
    # whole thread comes as one unit, and the next page starts after it.
    assert len(first["items"]) == 3 and first["next_cursor"]
    rest = client.list_messages(conversation="#support", max_results=2, cursor=first["next_cursor"])
    assert [i["preview"] for i in rest["items"]] == ["unrelated earlier post"]


def test_thread_expansion_is_bounded_per_call():
    import app.data_sources.clients.slack_messages_client as mod

    fake = FakeSlack()
    roots = [f"17000{n:05d}.000100" for n in range(40)]
    fake.routes["conversations.history"] = lambda q: {"ok": True, "has_more": False, "messages": [
        _msg(ts, thread_ts=ts, reply_count=1) for ts in roots]}
    fake.routes["conversations.replies"] = lambda q: {"ok": True, "has_more": False, "messages": [
        _msg(q["ts"], thread_ts=q["ts"], reply_count=1)]}
    _client(fake).list_messages(conversation="#support")
    assert fake.count("conversations.replies") == mod._MAX_THREAD_EXPANSIONS


# ------------------------------------------------ broadened search (Teams parity)


def test_broadened_terms_skip_stopwords_and_search_syntax():
    from app.data_sources.clients.slack_messages_client import slack_broadened_terms

    assert slack_broadened_terms("what drove revenue growth") == ["revenue", "growth", "drove"]
    assert slack_broadened_terms("למה החיבורים נפלו") == ["החיבורים", "נפלו"]
    assert slack_broadened_terms("AWS") is None
    assert slack_broadened_terms("churn in:#support") is None
    assert slack_broadened_terms('"exact phrase" here') is None


def test_an_empty_search_retries_each_word_and_ranks_messages_matching_more_words():
    fake = FakeSlack()
    hits = {
        "connections": [_match("C0SUPPORT1", "1700000100.000100", text="connections down"),
                        _match("C0SUPPORT1", "1700000200.000100", text="connections dropped after IAM change")],
        "dropped": [_match("C0SUPPORT1", "1700000200.000100", text="connections dropped after IAM change")],
    }

    def search(q):
        return {"ok": True, "messages": {"matches": hits.get(q["query"], []), "paging": {"pages": 1}}}

    fake.routes["search.messages"] = search
    fake.routes["conversations.replies"] = lambda q: {"ok": True, "has_more": False, "messages": [_msg(q["ts"])]}
    out = _client(fake).search_messages("why connections dropped")
    queries = [q["query"] for m, q in fake.calls if m == "search.messages"]
    assert queries == ["why connections dropped", "connections", "dropped"]
    assert out["broadened_query"] == "connections OR dropped"
    # The message matching both words ranks first; duplicates are merged.
    assert [i["id"] for i in out["items"]] == ["slack:C0SUPPORT1/1700000200.000100",
                                               "slack:C0SUPPORT1/1700000100.000100"]
    assert out["next_cursor"] is None


def test_a_search_with_results_is_not_broadened():
    fake = FakeSlack()
    fake.routes["search.messages"] = lambda q: {"ok": True, "messages": {
        "matches": [_match("C0SUPPORT1", "1700000100.000100")], "paging": {"pages": 1}}}
    fake.routes["conversations.replies"] = lambda q: {"ok": True, "has_more": False, "messages": [_msg(q["ts"])]}
    out = _client(fake).search_messages("churn rate")
    assert fake.count("search.messages") == 1 and "broadened_query" not in out


# ------------------------------------------------- search hit context (Teams parity)


def test_a_reply_hit_carries_the_post_it_answers_and_its_neighbours():
    fake = FakeSlack()
    root = "1700000100.000100"
    replies = [f"17000002{i}0.000100" for i in range(6)]
    hit = replies[3]
    fake.routes["search.messages"] = lambda q: {"ok": True, "messages": {
        "matches": [_match("C0SUPPORT1", hit, text="it was the IAM role", thread_ts=root)], "paging": {"pages": 1}}}

    def thread(q):
        assert q["ts"] == root  # the thread is read from its root
        return {"ok": True, "has_more": False, "messages": [_msg(root, text="3 connections are down", reply_count=6,
                                                               thread_ts=root)]
                + [_msg(ts, user="U0OMER0001", text=f"reply {i}", thread_ts=root) for i, ts in enumerate(replies)]}

    fake.routes["conversations.replies"] = thread
    row = _client(fake).search_messages("IAM role")["items"][0]
    assert row["is_reply"] is True and row["reply_count"] == 6
    assert row["thread"] == "Dana: 3 connections are down"
    assert row["context"] == ["↑ Omer: reply 1", "↑ Omer: reply 2", "↓ Omer: reply 4", "↓ Omer: reply 5"]
    assert row["more_after"] == 0


def test_a_post_hit_shows_its_first_replies_and_an_unthreaded_hit_shows_nothing_extra():
    fake = FakeSlack()
    posted, lone = "1700000100.000100", "1700000900.000100"
    fake.routes["search.messages"] = lambda q: {"ok": True, "messages": {
        "matches": [_match("C0SUPPORT1", posted), _match("C0SUPPORT1", lone)], "paging": {"pages": 1}}}
    replies = [f"17000002{i}0.000100" for i in range(5)]
    fake.routes["conversations.replies"] = lambda q: {"ok": True, "has_more": False, "messages": (
        [_msg(posted, reply_count=5, thread_ts=posted)] + [_msg(ts, text=f"r{i}", thread_ts=posted)
                                                           for i, ts in enumerate(replies)]
        if q["ts"] == posted else [_msg(lone)])}
    post, single = _client(fake).search_messages("churn")["items"]
    assert post["reply_count"] == 5 and post["context"] == ["↓ Dana: r0", "↓ Dana: r1", "↓ Dana: r2"]
    assert post["more_after"] == 2 and "thread" not in post
    assert "context" not in single and not single.get("reply_count")


def test_hit_context_is_bounded_and_never_fails_the_search():
    import app.data_sources.clients.slack_messages_client as mod

    fake = FakeSlack()
    fake.routes["search.messages"] = lambda q: {"ok": True, "messages": {
        "matches": [_match("C0SUPPORT1", f"17000{n:05d}.000100") for n in range(25)], "paging": {"pages": 1}}}
    fake.routes["conversations.replies"] = lambda q: {"ok": False, "error": "thread_not_found"}
    out = _client(fake).search_messages("churn")
    assert len(out["items"]) == 25
    assert fake.count("conversations.replies") == mod._SEARCH_CONTEXT_THREADS



# ------------------------------------------- per-agent scope: search scoping


def _search_router(fake, by_query):
    fake.routes["search.messages"] = lambda q: {"ok": True, "messages": {
        "matches": by_query(q["query"]), "paging": {"pages": 1}}}
    fake.routes["conversations.replies"] = lambda q: {"ok": True, "has_more": False, "messages": [_msg(q["ts"])]}


def test_several_allowed_channels_are_searched_with_slack_side_in_filters():
    fake = FakeSlack()
    _search_router(fake, lambda query: [_match("G0PRIVATE1", "1700000100.000100", name="leadership")]
                   if "in:#support in:#leadership" in query else [])
    out = _client(fake, allowed_channels=["C0SUPPORT1", "G0PRIVATE1"], include_direct_messages=False) \
        .search_messages("churn")
    assert [q["query"] for m, q in fake.calls if m == "search.messages"] == ["churn in:#support in:#leadership"]
    assert [i["conversation_id"] for i in out["items"]] == ["slack:G0PRIVATE1"]


def test_if_slack_ands_the_in_filters_the_search_falls_back_to_filtering_by_id():
    fake = FakeSlack()
    _search_router(fake, lambda query: [] if "in:" in query else [
        _match("C0SALES001", "1700000300.000100", name="sales"),
        _match("C0SUPPORT1", "1700000200.000100")])
    out = _client(fake, allowed_channels=["C0SUPPORT1", "G0PRIVATE1"], include_direct_messages=False) \
        .search_messages("churn")
    assert [q["query"] for m, q in fake.calls if m == "search.messages"][:2] == [
        "churn in:#support in:#leadership", "churn"]
    assert [i["conversation_id"] for i in out["items"]] == ["slack:C0SUPPORT1"]


def test_with_dms_in_scope_the_search_is_not_narrowed_by_in_filters():
    fake = FakeSlack()
    _search_router(fake, lambda query: [_match("D0DIRECT01", "1700000100.000100", name="U0DANA0001"),
                                        _match("C0SALES001", "1700000300.000100", name="sales")])
    out = _client(fake, allowed_channels=["C0SUPPORT1"]).search_messages("churn")
    assert [q["query"] for m, q in fake.calls if m == "search.messages"] == ["churn"]
    # The DM is kept (DMs on), the channel outside the scope is not.
    assert [i["conversation_id"] for i in out["items"]] == ["slack:D0DIRECT01"]


def test_a_renamed_channel_stays_in_scope_because_the_scope_is_by_id():
    fake = FakeSlack()
    renamed = [dict(c) for c in CHANNELS]
    renamed[0]["name"] = "customer-care"  # C0SUPPORT1, formerly #support
    fake.routes["users.conversations"] = lambda q: {"ok": True, "channels": renamed,
                                                    "response_metadata": {"next_cursor": ""}}
    by_id = _client(fake, allowed_channels=["C0SUPPORT1"], include_direct_messages=False).list_conversations()
    assert [r["name"] for r in by_id] == ["#customer-care"]
    by_old_name = _client(fake, allowed_channels="#support", include_direct_messages=False).list_conversations()
    assert by_old_name == []


def test_a_token_rotation_refresh_reads_the_flat_user_token():
    """The code exchange nests the user token under authed_user; a rotation
    REFRESH answers flat, with token_type "user" at the top level."""
    from app.services.connection_oauth_service import normalize_slack_token_response

    tokens = normalize_slack_token_response({
        "ok": True, "access_token": "xoxe.xoxp-2", "token_type": "user",
        "refresh_token": "xoxe-2", "expires_in": 43200, "scope": "search:read"})
    assert tokens["access_token"] == "xoxe.xoxp-2" and tokens["refresh_token"] == "xoxe-2"
    assert tokens["expires_at"]
    # A flat BOT token is never taken for the user's.
    with pytest.raises(ValueError, match="User Token Scopes"):
        normalize_slack_token_response({"ok": True, "access_token": "xoxb-1", "token_type": "bot"})


def test_a_fanout_spends_a_small_thread_budget():
    import app.data_sources.clients.slack_messages_client as mod

    fake = FakeSlack()
    fake.routes["conversations.history"] = lambda q: {"ok": True, "has_more": False, "messages": [
        _msg(f"17000{n:05d}.000100", thread_ts=f"17000{n:05d}.000100", reply_count=1) for n in range(10)]}
    fake.routes["conversations.replies"] = lambda q: {"ok": True, "has_more": False, "messages": [_msg(q["ts"])]}
    _client(fake).list_messages()  # no conversation: fan out
    assert fake.count("conversations.replies") <= mod._FANOUT_THREAD_EXPANSIONS
    assert fake.count("conversations.history") <= mod._FANOUT_CONVERSATIONS
