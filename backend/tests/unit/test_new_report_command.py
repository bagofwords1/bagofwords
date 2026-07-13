"""Unit tests for the "new"/"חדש" start-a-fresh-report command.

Exercises ExternalPlatformManager._process_verified_message with mocked
services/DB so the report-selection branches are tested in isolation (no DB
fixtures needed). A lone "new" (Teams 1:1 / WhatsApp only) must force-create a
report, confirm it, and create no completion; the next message must then
continue that fresh report.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.external_platform_manager import ExternalPlatformManager


def _make_manager():
    m = ExternalPlatformManager()
    m.mapping_service.get_user_by_id = AsyncMock(
        return_value=SimpleNamespace(id="u1", name="Alice")
    )
    m.organization_service.get_organization = AsyncMock(
        return_value=SimpleNamespace(id="org1")
    )
    m.completion_service.create_completion = AsyncMock()
    return m


def _data(text, platform="teams", channel_type="personal", is_thread_reply=False):
    return {
        "platform_type": platform,
        "message_text": text,
        "thread_ts": "conv-123",
        "message_ts": "msg-1",
        "channel_id": "conv-123",
        "channel_type": channel_type,
        "is_thread_reply": is_thread_reply,
    }


def _mapping(platform="teams"):
    return SimpleNamespace(
        app_user_id="u1",
        organization_id="org1",
        external_user_id="ext-user-1",
        platform_type=platform,
        platform_id="plat-1",
    )


def _adapter():
    return SimpleNamespace(add_reaction=AsyncMock(), send_dm_in_thread=AsyncMock())


def _db():
    db = MagicMock()
    # execute() resolves to a result whose scalar_one_or_none() is None, so the
    # org-settings lookup for the session staleness window falls back to defaults.
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=None)
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


@pytest.mark.parametrize(
    "platform,text,expected",
    [
        ("teams", "new", True),
        ("teams", "New", True),
        ("teams", "  new  ", True),
        ("teams", "חדש", True),
        ("teams", "  חדש ", True),
        ("whatsapp", "new", True),
        ("whatsapp", "חדש", True),
        # Only the lone keyword counts.
        ("teams", "new report", False),
        ("teams", "retry new", False),
        ("teams", "create a new dashboard", False),
        ("teams", "newer", False),
        ("whatsapp", "new.", False),
        ("teams", "", False),
        ("teams", None, False),
        # Platforms without cross-message report reuse never treat it as a command.
        ("slack", "new", False),
        ("email", "new", False),
    ],
)
def test_is_new_conversation_command(platform, text, expected):
    assert (
        ExternalPlatformManager._is_new_conversation_command(platform, text) is expected
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["teams", "whatsapp"])
async def test_new_command_forces_report_and_skips_completion(platform):
    m = _make_manager()
    adapter = _adapter()
    fresh = SimpleNamespace(id="R_NEW", title="Chat with Alice")
    keyword = "new" if platform == "teams" else "חדש"

    with patch.object(
        m, "_get_or_create_conversation_report", new=AsyncMock(return_value=(fresh, True))
    ) as goc, patch.object(
        m, "_find_recent_platform_report", new=AsyncMock()
    ) as frp, patch.object(
        m, "_find_report_by_thread_ts", new=AsyncMock()
    ) as fbt:
        res = await m._process_verified_message(
            _db(), adapter, _data(keyword, platform=platform), _mapping(platform)
        )

    assert res["action"] == "new_conversation_started"
    # Reuse is bypassed and a brand-new report is forced.
    assert goc.call_args.kwargs.get("force_new") is True
    assert frp.await_count == 0 and fbt.await_count == 0
    # The user is told a fresh report started, but no prompt is dispatched.
    assert adapter.send_dm_in_thread.await_count == 1
    assert m.completion_service.create_completion.await_count == 0


@pytest.mark.asyncio
async def test_teams_followup_continues_fresh_report():
    """After "new", the next Teams 1:1 message reuses the fresh (completion-less)
    report via report-level lookup, and its completion lands in that report."""
    m = _make_manager()
    fresh = SimpleNamespace(id="R_NEW", title="Chat with Alice")

    with patch.object(
        m, "_find_recent_platform_report", new=AsyncMock(return_value=fresh)
    ) as frp, patch.object(
        m, "_get_or_create_conversation_report", new=AsyncMock()
    ) as goc, patch(
        "app.services.report_service.ReportService"
    ) as RS:
        RS.return_value.set_data_sources_for_report = AsyncMock()
        m.data_source_service.get_active_data_sources = AsyncMock(return_value=[])
        res = await m._process_verified_message(
            _db(), _adapter(), _data("show me sales"), _mapping()
        )

    assert res["action"] == "message_processed"
    assert frp.await_count == 1          # report-level reuse used
    assert goc.await_count == 0          # no new report created
    assert m.completion_service.create_completion.await_count == 1
    assert (
        m.completion_service.create_completion.await_args.kwargs.get("report_id")
        == "R_NEW"
    )


@pytest.mark.asyncio
async def test_new_report_phrase_is_a_normal_prompt():
    """"new report" is a question, not the command — it must reach a completion."""
    m = _make_manager()
    fresh = SimpleNamespace(id="R_X", title="Chat with Alice")

    with patch.object(
        m, "_find_recent_platform_report", new=AsyncMock(return_value=fresh)
    ), patch.object(
        m, "_get_or_create_conversation_report", new=AsyncMock()
    ), patch(
        "app.services.report_service.ReportService"
    ) as RS:
        RS.return_value.set_data_sources_for_report = AsyncMock()
        m.data_source_service.get_active_data_sources = AsyncMock(return_value=[])
        res = await m._process_verified_message(
            _db(), _adapter(), _data("new report"), _mapping()
        )

    assert res["action"] == "message_processed"
    assert m.completion_service.create_completion.await_count == 1
