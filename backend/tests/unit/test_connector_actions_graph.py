"""Outlook and Teams write actions against a fake Microsoft 365.

The Graph boundary is faked (``tests/mocks/fake_microsoft365.py``, which
enforces delegated scopes the way Graph does); the clients run for real.
Contracts:

- each action sends exactly the Graph write it names, once;
- a token that lacks the action's scope is refused before anything is sent,
  with a message naming the missing permission;
- Teams posts honour the connection's allowed-channel list;
- every action declares an effect, and the effect sets its safe defaults.
"""
import httpx
import pytest

from app.data_sources.clients.connector_actions import (
    ALLOWED_POLICIES, EFFECT_DEFAULTS, EFFECTS, effect_from_hints, missing_scopes,
)
from app.data_sources.clients.graph_mail_client import GraphMailClient
from app.data_sources.clients.graph_teams_client import GraphTeamsClient
from tests.mocks.fake_microsoft365 import FakeM365, make_token

pytestmark = pytest.mark.unit


def _client(cls, fake, scopes, **kw):
    c = cls(access_token=make_token(scopes), **kw)
    c._user_token_provided = True
    c._http = httpx.Client(transport=httpx.MockTransport(fake))
    return c


@pytest.fixture
def fake():
    return FakeM365()


def _inbox_ids(fake):
    return [m["id"] for m in fake.messages.values() if m["parentFolderId"] == "fld-inbox"]


def test_flag_and_mark_read_send_one_patch(fake):
    c = _client(GraphMailClient, fake, ["Mail.ReadWrite"])
    mid = _inbox_ids(fake)[0]
    r = c.call_tool("flag_email", {"message_id": mid, "flag": "flagged", "is_read": True})
    assert r["success"], r
    assert fake.writes == [{"op": "update", "id": mid, "changes": {"flag": {"flagStatus": "flagged"}, "isRead": True}}]
    assert fake.messages[mid]["flag"]["flagStatus"] == "flagged" and fake.messages[mid]["isRead"] is True


@pytest.mark.parametrize("destination, folder_id", [("archive", "fld-archive"), ("Inbox/Invoices", "fld-invoices"),
                                                    ("Invoices", "fld-invoices")])
def test_move_resolves_the_folder_and_returns_the_new_id(fake, destination, folder_id):
    c = _client(GraphMailClient, fake, ["Mail.ReadWrite"])
    mid = _inbox_ids(fake)[0]
    r = c.call_tool("move_email", {"message_id": mid, "destination": destination})
    assert r["success"], r
    new_id = r["data"]["new_message_id"]
    assert new_id and new_id != mid and fake.messages[new_id]["parentFolderId"] == folder_id
    assert [w["op"] for w in fake.writes] == ["move"]


def test_draft_then_send_needs_both_scopes_and_sends_once(fake):
    c = _client(GraphMailClient, fake, ["Mail.ReadWrite", "Mail.Send"])
    mid = _inbox_ids(fake)[0]
    d = c.call_tool("create_draft", {"mode": "reply", "reply_to_message_id": mid, "body": "Paid on Friday."})
    assert d["success"] and d["data"]["sent"] is False, d
    s = c.call_tool("send_email", {"draft_id": d["data"]["draft_id"]})
    assert s["success"], s
    assert [w["op"] for w in fake.writes] == ["createReply", "send_draft"]


def test_send_new_message_uses_sendmail(fake):
    c = _client(GraphMailClient, fake, ["Mail.Send"])
    r = c.call_tool("send_email", {"to": ["lee@contoso.test"], "subject": "Hi", "body": "Hello"})
    assert r["success"], r
    assert fake.writes == [{"op": "send_mail", "subject": "Hi", "to": ["lee@contoso.test"]}]


@pytest.mark.parametrize("tool, args, granted, missing", [
    ("send_email", {"to": ["x@y.test"], "subject": "s", "body": "b"}, ["Mail.Read", "Mail.ReadWrite"], "Mail.Send"),
    ("flag_email", {"message_id": "any", "flag": "flagged"}, ["Mail.Read"], "Mail.ReadWrite"),
    ("move_email", {"message_id": "any", "destination": "archive"}, ["Mail.Read", "Mail.Send"], "Mail.ReadWrite"),
])
def test_a_token_without_the_scope_is_refused_before_anything_is_sent(fake, tool, args, granted, missing):
    c = _client(GraphMailClient, fake, granted)
    r = c.call_tool(tool, args)
    assert r["success"] is False and missing in r["error"], r
    assert fake.writes == []


def test_a_graph_403_is_reported_not_retried(fake):
    # A token whose claims cannot be read: permissions unknown, so the call is
    # sent; Graph's 403 comes back as the answer, after exactly one attempt.
    c = GraphMailClient(access_token="opaque-token")
    c._user_token_provided = True
    c._http = httpx.Client(transport=httpx.MockTransport(fake))
    r = c.call_tool("send_email", {"to": ["a@b.test"], "subject": "s", "body": "b"})
    assert r["success"] is False and "403" in r["error"], r
    assert sum(1 for x in fake.requests if "sendMail" in x) == 1


def test_teams_post_and_reply_go_to_the_resolved_channel(fake):
    c = _client(GraphTeamsClient, fake, ["ChannelMessage.Send", "Team.ReadBasic.All", "Channel.ReadBasic.All", "Chat.Read"])
    r = c.call_tool("post_message", {"channel": "Alerts", "text": "Closed 3 false positives."})
    assert r["success"], r
    root = fake.channel_posts["chan-alerts"][0]["id"]
    r2 = c.call_tool("post_message", {"channel": "SOC › Alerts", "text": "Details in thread", "reply_to_message_id": root})
    assert r2["success"], r2
    assert [(w["op"], w["channel"], w["reply_to"]) for w in fake.writes] == [
        ("channel_post", "Alerts", None), ("channel_post", "Alerts", root)]


def test_teams_posts_only_to_allowed_channels(fake):
    c = _client(GraphTeamsClient, fake, ["ChannelMessage.Send"], allowed_post_channels="Alerts")
    assert c.call_tool("post_message", {"channel": "Alerts", "text": "ok"})["success"]
    r = c.call_tool("post_message", {"channel": "General", "text": "nope"})
    assert r["success"] is False and "not allowed" in r["error"], r
    assert [w["channel"] for w in fake.writes] == ["Alerts"]


def test_teams_chat_message_and_wrong_kind(fake):
    c = _client(GraphTeamsClient, fake, ["ChatMessage.Send", "ChannelMessage.Send"])
    assert c.call_tool("send_chat_message", {"chat": "Lee Ops", "text": "ping"})["success"]
    wrong = c.call_tool("send_chat_message", {"chat": "Alerts", "text": "x"})
    assert wrong["success"] is False
    assert [w["op"] for w in fake.writes] == ["chat_message"]


def test_teams_post_without_scope_is_refused(fake):
    c = _client(GraphTeamsClient, fake, ["Chat.Read"])
    r = c.call_tool("post_message", {"channel": "Alerts", "text": "x"})
    assert r["success"] is False and "ChannelMessage.Send" in r["error"]
    assert fake.writes == []


@pytest.mark.parametrize("cls", [GraphMailClient, GraphTeamsClient])
def test_every_action_declares_an_effect_and_a_scope(cls):
    assert cls.actions
    for a in cls.actions:
        assert a.effect in EFFECTS and a.effect != "read"
        assert a.required_scopes, a.name
        assert a.input_schema.get("type") == "object"
        assert isinstance(a.preview({}), str)


def test_effect_defaults_never_start_a_write_on_and_limit_policies():
    for effect in ("modify", "external", "destructive"):
        enabled, policy = EFFECT_DEFAULTS[effect]
        assert enabled is False and policy in ALLOWED_POLICIES[effect]
    assert "auto" not in ALLOWED_POLICIES["external"]
    assert "allow" not in ALLOWED_POLICIES["destructive"]


@pytest.mark.parametrize("hints, effect", [
    ({"readOnlyHint": True}, "read"), ({"destructiveHint": True}, "destructive"),
    ({"openWorldHint": True}, "external"), ({"readOnlyHint": False}, "modify"), ({}, None),
])
def test_mcp_effect_from_annotations(hints, effect):
    assert effect_from_hints({"annotations": hints}) == effect


def test_missing_scopes_accepts_any_alternative():
    post = GraphTeamsClient.action_by_name("post_message")
    assert missing_scopes(post, {"Group.ReadWrite.All"}) == []
    assert missing_scopes(post, {"Chat.Read"}) == ["ChannelMessage.Send"]
    assert missing_scopes(post, None) == []
