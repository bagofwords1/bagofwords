"""Unit tests for delivering a run FAILURE back to the chat channel.

When a Slack / WhatsApp / Google Chat run flips to ``status='error'`` it has no
answer blocks to send, so without ``send_completion_error_to_slack`` the user who
asked is left with a 👀 reaction and silence. These tests exercise the branch
logic and the adapter call shape deterministically — no network, no real DB.

Contract under test:
  * the SAME failure text BOW shows in its web chat (``{summary}:
    {provider_message}``) is delivered in the originating thread,
  * the 👀 working reaction is swapped for ❌,
  * non-chat / unconfigured platforms send nothing,
  * a repeat finish-update does not re-send.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import completion_block as cb
from app.services import channel_error_strings as ces


@pytest.fixture(autouse=True)
def _clear_dedupe_guards():
    """The in-process guards are module-level sets; isolate each test."""
    cb._error_sent_completion_ids.clear()
    cb._swapped_completion_ids.clear()
    yield
    cb._error_sent_completion_ids.clear()
    cb._swapped_completion_ids.clear()


class _AsyncCM:
    def __init__(self, obj):
        self._obj = obj

    async def __aenter__(self):
        return self._obj

    async def __aexit__(self, *exc):
        return False


def _db_returning(*results):
    """An AsyncMock db whose execute() yields one result per call, in order."""
    db = AsyncMock()
    outs = []
    for value in results:
        r = MagicMock()
        r.scalar_one_or_none.return_value = value
        outs.append(r)
    db.execute = AsyncMock(side_effect=outs)
    return db


def _completion(
    *,
    platform="slack",
    user="U123",
    error_payload=None,
    channel_type="im",
    channel_id="C1",
    message_ts="1700000000.1",
    thread_ts="1700000000.0",
    parent_id="head-1",
    model=None,
):
    return SimpleNamespace(
        external_platform=platform,
        external_user_id=user,
        external_thread_ts=thread_ts,
        external_message_ts=message_ts,
        external_channel_id=channel_id,
        external_channel_type=channel_type,
        parent_id=parent_id,
        completion=error_payload,
        model=model,
        report=SimpleNamespace(organization_id="org-1"),
    )


def _org(locale="he"):
    return SimpleNamespace(id="org-1", settings=SimpleNamespace(config={"locale": locale}))


def _platform(ptype="slack"):
    return SimpleNamespace(platform_type=ptype, organization_id="org-1")


async def _run(completion, *, org=..., platform=..., prompt_row=None, adapter=None, comp_id="sys-1"):
    """Drive send_completion_error_to_slack with a mocked session + adapter.

    execute() order inside the function: completion, Organization, ExternalPlatform.
    Returns (adapter, db).
    """
    org = _org() if org is ... else org
    platform = _platform(completion.external_platform) if platform is ... else platform
    db = _db_returning(completion, org, platform, prompt_row)
    adapter = adapter or AsyncMock()

    session_maker = MagicMock(return_value=_AsyncCM(db))
    with patch.object(cb, "create_async_session_factory", return_value=session_maker), \
         patch.object(cb.PlatformAdapterFactory, "create_adapter", return_value=adapter):
        await cb.send_completion_error_to_slack(comp_id)
    return adapter, db


def _only_message(adapter):
    """The single text delivered to the channel."""
    adapter.send_dm_in_thread.assert_awaited_once()
    args, kwargs = adapter.send_dm_in_thread.call_args
    return args[1]


# --- bow_error_message: parity with BOW's own web display -------------------


def test_summary_and_provider_message_are_joined_like_the_web():
    err = {"summary": "anthropic call failed",
           "provider_message": "LLM v2 streaming failed (provider=anthropic): Connection error."}
    assert ces.bow_error_message(err) == (
        "anthropic call failed: LLM v2 streaming failed (provider=anthropic): Connection error."
    )


def test_equal_or_missing_parts_collapse_to_one():
    # equal summary and provider_message → not duplicated
    same = "Overloaded"
    assert ces.bow_error_message({"summary": same, "provider_message": same}) == same
    # only provider_message
    assert ces.bow_error_message({"provider_message": "Connection error."}) == "Connection error."
    # only summary
    assert ces.bow_error_message({"summary": "anthropic call failed"}) == "anthropic call failed"


def test_raw_tail_and_message_back_up_provider_message():
    assert ces.bow_error_message({"raw_tail": "boom tail"}) == "boom tail"
    assert ces.bow_error_message({"message": "boom msg"}) == "boom msg"
    # provider_message wins over the backups
    assert ces.bow_error_message(
        {"provider_message": "real", "raw_tail": "tail", "message": "msg"}
    ) == "real"


def test_bare_bool_error_falls_back_to_content_then_minimum():
    # The background recovery path stores {'error': True} (a bool, not a dict).
    assert ces.bow_error_message(True, content="Agent failed: boom") == "Agent failed: boom"
    # nothing usable anywhere → a minimal line, never an empty string
    out = ces.bow_error_message(True, content=None)
    assert out and out == ces._FALLBACK
    assert ces.bow_error_message(None, content="") == ces._FALLBACK


# --- delivery through send_completion_error_to_slack ------------------------


@pytest.mark.asyncio
async def test_error_sends_bow_text_in_origin_thread_and_swaps_reaction():
    err = {"summary": "anthropic quota or credit balance exhausted",
           "provider_message": "Your credit balance is too low to access the API"}
    comp = _completion(error_payload={"error": err})
    adapter, _ = await _run(comp, comp_id="deliver")

    msg = _only_message(adapter)
    assert msg == ces.bow_error_message(err)
    # routed to the originating thread
    assert adapter.send_dm_in_thread.call_args[0][2] == "1700000000.0"  # thread_ts
    # 👀 → ❌ so the working marker never hangs
    adapter.remove_reaction.assert_awaited_once()
    assert adapter.remove_reaction.call_args[0][2] == "eyes"
    adapter.add_reaction.assert_awaited_once()
    assert adapter.add_reaction.call_args[0][2] == "x"


@pytest.mark.asyncio
async def test_reported_connection_error_shows_raw_text_not_a_guess():
    # The reported case: a broken key surfaces as code="unknown" + "Connection
    # error.". We show BOW's exact raw text, never a guessed "network, try again".
    err = {
        "code": "unknown",
        "summary": "anthropic call failed",
        "provider_message": "LLM v2 streaming failed (provider=anthropic, model=claude-sonnet-5-5): Connection error.",
    }
    comp = _completion(error_payload={"content": "…", "error": err})
    adapter, _ = await _run(comp, comp_id="conn")
    msg = _only_message(adapter)
    assert msg == ("anthropic call failed: LLM v2 streaming failed "
                   "(provider=anthropic, model=claude-sonnet-5-5): Connection error.")


@pytest.mark.asyncio
async def test_bare_bool_payload_sends_the_content():
    comp = _completion(error_payload={"content": "Agent failed: boom", "error": True})
    adapter, _ = await _run(comp, comp_id="bare")
    assert _only_message(adapter) == "Agent failed: boom"


@pytest.mark.asyncio
async def test_channel_mention_routes_to_channel_not_dm():
    # A public-channel mention replies in the channel; a DM opens by user id.
    err = {"error": {"summary": "anthropic call failed", "provider_message": "boom"}}
    comp = _completion(channel_type="channel", channel_id="C-pub", error_payload=err)
    adapter, _ = await _run(comp, comp_id="pub")
    assert adapter.send_dm_in_thread.call_args.kwargs["channel_id"] == "C-pub"

    comp_dm = _completion(channel_type="im", channel_id="C-dm", error_payload=err)
    adapter_dm, _ = await _run(comp_dm, comp_id="dm")
    assert adapter_dm.send_dm_in_thread.call_args.kwargs["channel_id"] is None


@pytest.mark.asyncio
async def test_unsupported_platform_sends_nothing():
    # Teams reports failure via its own progress bubble; it must not be routed here.
    comp = _completion(platform="teams")
    db = _db_returning(comp)  # bails before Organization lookup
    adapter = AsyncMock()
    session_maker = MagicMock(return_value=_AsyncCM(db))
    with patch.object(cb, "create_async_session_factory", return_value=session_maker), \
         patch.object(cb.PlatformAdapterFactory, "create_adapter", return_value=adapter):
        await cb.send_completion_error_to_slack("sys-1")
    adapter.send_dm_in_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_platform_configured_sends_nothing():
    comp = _completion()
    adapter = AsyncMock()
    db = _db_returning(comp, _org(), None)  # platform row missing
    session_maker = MagicMock(return_value=_AsyncCM(db))
    with patch.object(cb, "create_async_session_factory", return_value=session_maker), \
         patch.object(cb.PlatformAdapterFactory, "create_adapter", return_value=adapter):
        await cb.send_completion_error_to_slack("sys-1")
    adapter.send_dm_in_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeat_finish_update_does_not_resend():
    comp = _completion(error_payload={"error": {"summary": "x", "provider_message": "boom"}})
    adapter, _ = await _run(comp, comp_id="dup")
    adapter.send_dm_in_thread.assert_awaited_once()

    # Second finish-update for the same completion id: guarded, no second send.
    adapter2 = AsyncMock()
    session_maker = MagicMock(return_value=_AsyncCM(_db_returning(comp)))
    with patch.object(cb, "create_async_session_factory", return_value=session_maker), \
         patch.object(cb.PlatformAdapterFactory, "create_adapter", return_value=adapter2):
        await cb.send_completion_error_to_slack("dup")
    adapter2.send_dm_in_thread.assert_not_awaited()
