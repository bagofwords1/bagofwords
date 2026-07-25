"""Unit tests for Slack Socket Mode: envelope processing and the shared
transport-agnostic event handler (slack_event_service)."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.slack_socket_service import SlackSocketClient
from app.services import slack_event_service


# ---------- SlackSocketClient.process_envelope ----------


def _client():
    return SlackSocketClient("xapp-test", handler=AsyncMock())


def test_hello_frame_is_ignored():
    ack, payload, reconnect = _client().process_envelope(json.dumps({"type": "hello"}))
    assert (ack, payload, reconnect) == (None, None, False)


def test_disconnect_frame_requests_reconnect():
    ack, payload, reconnect = _client().process_envelope(
        json.dumps({"type": "disconnect", "reason": "refresh_requested"})
    )
    assert reconnect is True and payload is None


def test_events_api_envelope_acks_and_extracts_payload():
    envelope = {
        "type": "events_api",
        "envelope_id": "env-1",
        "payload": {"type": "event_callback", "team_id": "T1", "event": {"type": "message"}},
    }
    ack, payload, reconnect = _client().process_envelope(json.dumps(envelope))
    assert ack == {"envelope_id": "env-1"}
    assert payload["team_id"] == "T1"
    assert reconnect is False


def test_interactive_envelope_is_acked_but_dropped():
    envelope = {"type": "interactive", "envelope_id": "env-2", "payload": {"foo": 1}}
    ack, payload, _ = _client().process_envelope(json.dumps(envelope))
    assert ack == {"envelope_id": "env-2"}
    assert payload is None


def test_garbage_frame_is_ignored():
    assert _client().process_envelope("not json") == (None, None, False)


# ---------- handle_slack_event ----------


def _platform():
    return SimpleNamespace(
        id="plat-1",
        organization_id="org-1",
        is_active=True,
        platform_type="slack",
        platform_config={"team_id": "T1", "connection_mode": "socket_mode"},
    )


def _db_with_platform(platform):
    db = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [platform] if platform else []
    db.execute = AsyncMock(return_value=result)
    return db


def _event(event, event_id="Ev1", team_id="T1", etype="event_callback"):
    return {"type": etype, "team_id": team_id, "event_id": event_id, "event": event}


@pytest.fixture(autouse=True)
def _fresh_dedupe():
    slack_event_service.processed_events.clear()
    slack_event_service._assistant_threads.clear()
    yield


@pytest.mark.asyncio
async def test_dm_message_reaches_manager():
    platform = _platform()
    with patch.object(
        slack_event_service.platform_manager, "handle_incoming_message",
        new=AsyncMock(return_value={"success": True}),
    ) as handoff:
        res = await slack_event_service.handle_slack_event(
            _db_with_platform(platform),
            _event({"type": "message", "channel_type": "im", "text": "hi", "channel": "D1"}),
        )
    assert res["ok"] is True and "result" in res
    assert handoff.await_count == 1


@pytest.mark.asyncio
async def test_bot_messages_and_duplicates_are_skipped():
    platform = _platform()
    db = _db_with_platform(platform)
    with patch.object(
        slack_event_service.platform_manager, "handle_incoming_message", new=AsyncMock()
    ) as handoff:
        res = await slack_event_service.handle_slack_event(
            db, _event({"type": "message", "channel_type": "im", "text": "x", "bot_id": "B9"})
        )
        assert res["skipped"] == "bot_message"

        ok_event = _event({"type": "message", "channel_type": "im", "text": "hi"}, event_id="EvDup")
        await slack_event_service.handle_slack_event(db, ok_event)
        res = await slack_event_service.handle_slack_event(db, ok_event)
        assert res["skipped"] == "duplicate"
    assert handoff.await_count == 1


@pytest.mark.asyncio
async def test_channel_message_without_mention_is_skipped():
    with patch.object(
        slack_event_service.platform_manager, "handle_incoming_message", new=AsyncMock()
    ) as handoff:
        res = await slack_event_service.handle_slack_event(
            _db_with_platform(_platform()),
            _event({"type": "message", "channel_type": "channel", "text": "hi"}),
        )
    assert res["skipped"] == "non_dm_message"
    assert handoff.await_count == 0


@pytest.mark.asyncio
async def test_assistant_thread_started_sets_prompts_and_tracks_thread():
    platform = _platform()
    adapter = SimpleNamespace(
        set_suggested_prompts=AsyncMock(return_value=True),
        set_assistant_status=AsyncMock(return_value=True),
    )
    with patch.object(
        slack_event_service.PlatformAdapterFactory, "create_adapter", return_value=adapter
    ), patch.object(
        slack_event_service, "_suggested_prompts_for_org",
        new=AsyncMock(return_value=["Top customers?"]),
    ):
        res = await slack_event_service.handle_slack_event(
            _db_with_platform(platform),
            _event({
                "type": "assistant_thread_started",
                "assistant_thread": {"channel_id": "D1", "thread_ts": "111.222"},
            }),
        )
    assert res["action"] == "assistant_thread_started"
    adapter.set_suggested_prompts.assert_awaited_once()
    assert slack_event_service.is_assistant_thread("D1", "111.222")


@pytest.mark.asyncio
async def test_message_in_assistant_thread_sets_status():
    platform = _platform()
    slack_event_service._assistant_threads.add("D1:111.222")
    adapter = SimpleNamespace(set_assistant_status=AsyncMock(return_value=True))
    with patch.object(
        slack_event_service.PlatformAdapterFactory, "create_adapter", return_value=adapter
    ), patch.object(
        slack_event_service.platform_manager, "handle_incoming_message",
        new=AsyncMock(return_value={"success": True}),
    ):
        await slack_event_service.handle_slack_event(
            _db_with_platform(platform),
            _event({
                "type": "message", "channel_type": "im", "text": "hi",
                "channel": "D1", "thread_ts": "111.222",
            }),
        )
    adapter.set_assistant_status.assert_awaited_once_with("D1", "111.222", "is thinking…")


@pytest.mark.asyncio
async def test_app_home_opened_pushes_prompts_without_thread():
    """agent_view (2026): app_home_opened(tab=messages) is the entry signal;
    prompts are set at Messages-tab level (thread_ts=None)."""
    adapter = SimpleNamespace(set_suggested_prompts=AsyncMock(return_value=True))
    with patch.object(
        slack_event_service.PlatformAdapterFactory, "create_adapter", return_value=adapter
    ), patch.object(
        slack_event_service, "_suggested_prompts_for_org",
        new=AsyncMock(return_value=["Top customers?"]),
    ):
        res = await slack_event_service.handle_slack_event(
            _db_with_platform(_platform()),
            _event({"type": "app_home_opened", "tab": "messages", "channel": "D1"}),
        )
    assert res["action"] == "app_home_opened"
    adapter.set_suggested_prompts.assert_awaited_once_with("D1", None, ["Top customers?"])


@pytest.mark.asyncio
async def test_top_level_dm_sets_status_on_own_ts():
    """agent_view: setStatus fires for every DM using the effective thread
    (the message's own ts for top-level messages)."""
    adapter = SimpleNamespace(set_assistant_status=AsyncMock(return_value=True))
    with patch.object(
        slack_event_service.PlatformAdapterFactory, "create_adapter", return_value=adapter
    ), patch.object(
        slack_event_service.platform_manager, "handle_incoming_message",
        new=AsyncMock(return_value={"success": True}),
    ):
        await slack_event_service.handle_slack_event(
            _db_with_platform(_platform()),
            _event({"type": "message", "channel_type": "im", "text": "hi",
                    "channel": "D1", "ts": "222.333"}),
        )
    adapter.set_assistant_status.assert_awaited_once_with("D1", "222.333", "is thinking…")


@pytest.mark.asyncio
async def test_unknown_team_is_ignored():
    with patch.object(
        slack_event_service.platform_manager, "handle_incoming_message", new=AsyncMock()
    ) as handoff:
        res = await slack_event_service.handle_slack_event(
            _db_with_platform(None),
            _event({"type": "message", "channel_type": "im", "text": "hi"}),
        )
    assert res["skipped"] == "no_platform"
    assert handoff.await_count == 0
