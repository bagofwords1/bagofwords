"""Unit tests for Teams channel -> agent routing at the adapter boundary.

Teams is the second same-connection platform (after Slack) to carry multiple
channels under one bot. Two adapter-level contracts make the shared routing
pipeline work for Teams:

* ``process_incoming_message`` must surface the **stable** channel key from
  ``channelData.channel.id`` as ``routing_channel_id`` — NOT ``conversation.id``,
  which is thread-scoped (it carries ``;messageid=<root>``). Personal chats and
  ad-hoc group chats have no channel, so they must yield ``None`` and fall back
  to the connection default (the Teams analogue of a Slack DM).
* ``get_channel_info`` resolves a channel's display name via the Bot Connector
  (``/v3/teams/{team}/conversations``) using the bot's own token — no Graph
  consent — so discovery can label a channel ``#name`` instead of its id.
"""
import pytest

from app.services.platform_adapters.teams_adapter import TeamsAdapter


class _FakePlatform:
    def __init__(self, config=None, creds=None):
        self.platform_config = config or {
            "tenant_id": "tenant-1",
            "app_id": "app-123",
            "service_url": "https://smba.trafficmanager.net/teams/",
        }
        self._creds = creds or {"app_id": "app-123", "client_secret": "secret"}

    def decrypt_credentials(self):
        return self._creds


def _adapter():
    return TeamsAdapter(_FakePlatform())


def _channel_activity(channel_id, team_id="19:team@thread.tacv2"):
    # A real Teams channel message: conversation.id is the thread (channel id +
    # root message), while channelData.channel.id is the stable channel key.
    return {
        "type": "message",
        "text": "<at>BOW</at> which agents do you see?",
        "id": "activity-1",
        "from": {"id": "29:user-1"},
        "recipient": {"id": "28:app-123", "name": "BOW"},
        "conversation": {"conversationType": "channel", "id": f"{channel_id};messageid=root-1"},
        "channelData": {
            "channel": {"id": channel_id},
            "team": {"id": team_id},
            "tenant": {"id": "tenant-1"},
        },
        "serviceUrl": "https://smba.trafficmanager.net/teams/",
    }


@pytest.mark.asyncio
async def test_channel_activity_uses_stable_channel_id_not_conversation_id():
    ad = _adapter()
    cid = "19:abcdef0123@thread.tacv2"
    out = await ad.process_incoming_message(_channel_activity(cid))

    # Routing keys on the stable channel id, never the thread-scoped conversation id.
    assert out["routing_channel_id"] == cid
    assert out["channel_id"] != cid
    assert out["channel_id"].startswith(cid)  # conversation.id = channel + ;messageid
    assert out["teams_team_id"] == "19:team@thread.tacv2"


@pytest.mark.asyncio
async def test_personal_chat_has_no_routing_channel_id():
    ad = _adapter()
    activity = {
        "type": "message", "text": "hi", "id": "activity-2",
        "from": {"id": "29:user-1"}, "recipient": {"id": "28:app-123"},
        "conversation": {"conversationType": "personal", "id": "a:1personalthread"},
        "channelData": {"tenant": {"id": "tenant-1"}},  # no channel
        "serviceUrl": "https://smba.trafficmanager.net/teams/",
    }
    out = await ad.process_incoming_message(activity)
    assert out["routing_channel_id"] is None  # DM -> connection default


@pytest.mark.asyncio
async def test_group_chat_has_no_routing_channel_id():
    ad = _adapter()
    activity = {
        "type": "message", "text": "hi", "id": "activity-3",
        "from": {"id": "29:user-1"}, "recipient": {"id": "28:app-123"},
        "conversation": {"conversationType": "groupChat", "id": "19:group@thread.v2"},
        "channelData": {"tenant": {"id": "tenant-1"}},  # ad-hoc group: no channel
        "serviceUrl": "https://smba.trafficmanager.net/teams/",
    }
    out = await ad.process_incoming_message(activity)
    assert out["routing_channel_id"] is None


@pytest.mark.asyncio
async def test_get_channel_info_resolves_name_via_bot_connector(monkeypatch):
    ad = _adapter()
    cid = "19:abcdef0123@thread.tacv2"

    async def _fake_token():
        return "bot-token"
    monkeypatch.setattr(ad, "_get_access_token", _fake_token)

    class _Resp:
        status_code = 200

        def json(self):
            return {"conversations": [
                {"id": "19:other@thread.tacv2", "name": "general"},
                {"id": cid, "name": "sales"},
            ]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            assert "/v3/teams/" in url and url.endswith("/conversations")
            return _Resp()

    monkeypatch.setattr(
        "app.services.platform_adapters.teams_adapter.httpx.AsyncClient", _Client
    )

    info = await ad.get_channel_info(
        cid,
        {"teams_team_id": "19:team@thread.tacv2",
         "service_url": "https://smba.trafficmanager.net/teams/"},
    )
    assert info["name"] == "sales"


@pytest.mark.asyncio
async def test_get_channel_info_without_team_id_is_best_effort_noop():
    ad = _adapter()
    # No team id in context -> we can't list channels; return {} so the id
    # stays the label rather than raising into the message path.
    info = await ad.get_channel_info("19:x@thread.tacv2", {})
    assert info == {}
