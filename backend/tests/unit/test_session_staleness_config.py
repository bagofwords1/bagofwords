"""Unit tests for the org-configurable Teams/WhatsApp session staleness window.

Teams 1:1 and WhatsApp reuse a conversation report across messages; how long
that reuse window stays open is configurable per org (in hours) via
``teams_session_max_age_hours`` / ``whatsapp_session_max_age_hours`` on the
Channels settings page. These tests cover the settings lookup (defaults and
fallbacks), the threading of the configured hours into the report-selection
paths, and the update-side range validation.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.models.organization_settings import OrganizationSettings
from app.services.external_platform_manager import ExternalPlatformManager
from app.services.organization_settings_service import OrganizationSettingsService


def _db_returning_settings(settings_row):
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=settings_row)
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "platform,expected_default",
    [("teams", 120), ("whatsapp", 24)],
)
async def test_defaults_when_no_settings_row(platform, expected_default):
    m = ExternalPlatformManager()
    db = _db_returning_settings(None)
    assert await m._get_session_max_age_hours(db, "org1", platform) == expected_default


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "platform,expected_default",
    [("teams", 120), ("whatsapp", 24)],
)
async def test_defaults_when_key_absent_from_config(platform, expected_default):
    """A settings row without the key falls back to the schema default."""
    m = ExternalPlatformManager()
    db = _db_returning_settings(OrganizationSettings(config={}))
    assert await m._get_session_max_age_hours(db, "org1", platform) == expected_default


@pytest.mark.asyncio
async def test_configured_value_wins():
    m = ExternalPlatformManager()
    db = _db_returning_settings(
        OrganizationSettings(config={"teams_session_max_age_hours": 48})
    )
    assert await m._get_session_max_age_hours(db, "org1", "teams") == 48


@pytest.mark.asyncio
async def test_feature_config_shaped_value_is_unwrapped():
    m = ExternalPlatformManager()
    db = _db_returning_settings(
        OrganizationSettings(config={"whatsapp_session_max_age_hours": {"value": 36}})
    )
    assert await m._get_session_max_age_hours(db, "org1", "whatsapp") == 36


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_value", ["abc", 0, -5, None, {"value": "x"}])
async def test_invalid_stored_values_fall_back_to_default(bad_value):
    m = ExternalPlatformManager()
    db = _db_returning_settings(
        OrganizationSettings(config={"teams_session_max_age_hours": bad_value})
    )
    assert await m._get_session_max_age_hours(db, "org1", "teams") == 120


# --- threading into report selection -----------------------------------------


def _manager():
    m = ExternalPlatformManager()
    m.mapping_service.get_user_by_id = AsyncMock(
        return_value=SimpleNamespace(id="u1", name="Alice")
    )
    m.organization_service.get_organization = AsyncMock(
        return_value=SimpleNamespace(id="org1")
    )
    m.completion_service.create_completion = AsyncMock()
    return m


def _data(platform, channel_type):
    return {
        "platform_type": platform,
        "message_text": "show me sales",
        "thread_ts": "conv-123",
        "message_ts": "msg-1",
        "channel_id": "conv-123",
        "channel_type": channel_type,
        "is_thread_reply": False,
    }


def _mapping(platform):
    return SimpleNamespace(
        app_user_id="u1",
        organization_id="org1",
        external_user_id="ext-user-1",
        platform_type=platform,
        platform_id="plat-1",
    )


def _adapter():
    return SimpleNamespace(add_reaction=AsyncMock(), send_dm_in_thread=AsyncMock())


def _plain_db():
    return _db_returning_settings(None)


@pytest.mark.asyncio
async def test_teams_lookup_uses_configured_hours():
    m = _manager()
    fresh = SimpleNamespace(id="R1", title="Chat with Alice")

    with patch.object(
        m, "_get_session_max_age_hours", new=AsyncMock(return_value=7)
    ) as hrs, patch.object(
        m, "_find_recent_platform_report", new=AsyncMock(return_value=fresh)
    ) as frp, patch(
        "app.services.report_service.ReportService"
    ) as RS:
        RS.return_value.set_data_sources_for_report = AsyncMock()
        m.data_source_service.get_active_data_sources = AsyncMock(return_value=[])
        res = await m._process_verified_message(
            _plain_db(), _adapter(), _data("teams", "personal"), _mapping("teams")
        )

    assert res["action"] == "message_processed"
    assert hrs.await_args.args[1:] == ("org1", "teams")
    assert frp.await_args.kwargs.get("max_age_hours") == 7


@pytest.mark.asyncio
async def test_whatsapp_creation_receives_configured_hours():
    m = _manager()
    fresh = SimpleNamespace(id="R2", title="Chat with Alice")

    with patch.object(
        m, "_get_session_max_age_hours", new=AsyncMock(return_value=3)
    ) as hrs, patch.object(
        m, "_get_or_create_conversation_report", new=AsyncMock(return_value=(fresh, True))
    ) as goc:
        res = await m._process_verified_message(
            _plain_db(), _adapter(), _data("whatsapp", "im"), _mapping("whatsapp")
        )

    assert res["action"] == "message_processed"
    assert hrs.await_args.args[1:] == ("org1", "whatsapp")
    assert goc.await_args.kwargs.get("max_age_hours") == 3


@pytest.mark.asyncio
async def test_non_whatsapp_platforms_skip_settings_lookup():
    """Slack mints a fresh report per message — no window, no settings query."""
    m = _manager()
    fresh = SimpleNamespace(id="R3", title="Chat with Alice")

    with patch.object(
        m, "_get_session_max_age_hours", new=AsyncMock()
    ) as hrs, patch.object(
        m, "_get_or_create_conversation_report", new=AsyncMock(return_value=(fresh, True))
    ) as goc:
        res = await m._process_verified_message(
            _plain_db(), _adapter(), _data("slack", "im"), _mapping("slack")
        )

    assert res["action"] == "message_processed"
    assert hrs.await_count == 0
    assert goc.await_args.kwargs.get("max_age_hours") is None


# --- update-side validation ---------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["teams_session_max_age_hours", "whatsapp_session_max_age_hours"])
@pytest.mark.parametrize("bad_value", [0, -1, 721, "12", 3.5, True, None])
async def test_update_rejects_out_of_range_hours(key, bad_value):
    from app.schemas.organization_settings_schema import OrganizationSettingsUpdate

    service = OrganizationSettingsService()
    settings_row = OrganizationSettings(config={key: 24})

    with patch.object(service, "get_settings", new=AsyncMock(return_value=settings_row)):
        with pytest.raises(HTTPException) as exc:
            await service.update_settings(
                MagicMock(),
                SimpleNamespace(id="org1"),
                SimpleNamespace(id="u1"),
                OrganizationSettingsUpdate(config={key: bad_value}),
            )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_update_accepts_valid_hours():
    from app.schemas.organization_settings_schema import OrganizationSettingsUpdate

    service = OrganizationSettingsService()
    settings_row = OrganizationSettings(config={"teams_session_max_age_hours": 120})
    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    with patch.object(service, "get_settings", new=AsyncMock(return_value=settings_row)), \
         patch("app.services.organization_settings_service.audit_service") as audit:
        audit.log = AsyncMock()
        result = await service.update_settings(
            db,
            SimpleNamespace(id="org1"),
            SimpleNamespace(id="u1"),
            OrganizationSettingsUpdate(config={"teams_session_max_age_hours": 48}),
        )

    assert result.config["teams_session_max_age_hours"] == 48
    assert db.commit.await_count == 1


@pytest.mark.asyncio
async def test_update_normalizes_feature_shaped_payload():
    """A {'value': N} payload (ai_settings-style) is stored as the bare int."""
    from app.schemas.organization_settings_schema import OrganizationSettingsUpdate

    service = OrganizationSettingsService()
    settings_row = OrganizationSettings(config={"whatsapp_session_max_age_hours": 24})
    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    with patch.object(service, "get_settings", new=AsyncMock(return_value=settings_row)), \
         patch("app.services.organization_settings_service.audit_service") as audit:
        audit.log = AsyncMock()
        result = await service.update_settings(
            db,
            SimpleNamespace(id="org1"),
            SimpleNamespace(id="u1"),
            OrganizationSettingsUpdate(config={"whatsapp_session_max_age_hours": {"value": 12}}),
        )

    assert result.config["whatsapp_session_max_age_hours"] == 12
