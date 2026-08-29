"""Unit tests for the per-platform "announce new conversation report" toggle.

When a message on an external channel creates a fresh report, the bot replies
with "I've started a new conversation report" plus a link. Orgs can switch
that announcement off per platform via ``{platform}_announce_new_report``
(edited in each channel's integration modal). These tests cover the settings
lookup (defaults and fallbacks), the send/suppress behavior in the message
flow, and the update-side bool validation.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.models.organization_settings import OrganizationSettings
from app.services.external_platform_manager import ExternalPlatformManager
from app.services.organization_settings_service import OrganizationSettingsService

ANNOUNCE_KEYS = [
    "slack_announce_new_report",
    "teams_announce_new_report",
    "google_chat_announce_new_report",
    "email_announce_new_report",
]

ANNOUNCE_PLATFORMS = ["slack", "teams", "google_chat", "email"]


@pytest.fixture(autouse=True)
def _principal_belongs_to_org():
    """The manager re-checks org membership on every verified message; the
    mocked DB would fail that check and divert every test into the
    access-revoked path. Stub it to True."""
    with patch(
        "app.core.permission_resolver.principal_belongs_to_org",
        new=AsyncMock(return_value=True),
    ):
        yield


def _db_returning_settings(settings_row):
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=settings_row)
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


# --- settings lookup ----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ANNOUNCE_PLATFORMS)
async def test_defaults_to_true_when_no_settings_row(platform):
    m = ExternalPlatformManager()
    db = _db_returning_settings(None)
    assert await m._get_announce_new_report(db, "org1", platform) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ANNOUNCE_PLATFORMS)
async def test_defaults_to_true_when_key_absent_from_config(platform):
    m = ExternalPlatformManager()
    db = _db_returning_settings(OrganizationSettings(config={}))
    assert await m._get_announce_new_report(db, "org1", platform) is True


@pytest.mark.asyncio
async def test_configured_false_wins():
    m = ExternalPlatformManager()
    db = _db_returning_settings(
        OrganizationSettings(config={"slack_announce_new_report": False})
    )
    assert await m._get_announce_new_report(db, "org1", "slack") is False


@pytest.mark.asyncio
async def test_feature_config_shaped_value_is_unwrapped():
    m = ExternalPlatformManager()
    db = _db_returning_settings(
        OrganizationSettings(config={"teams_announce_new_report": {"value": False}})
    )
    assert await m._get_announce_new_report(db, "org1", "teams") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_value", ["abc", 0, 1, None, {"value": "x"}])
async def test_invalid_stored_values_fall_back_to_default(bad_value):
    m = ExternalPlatformManager()
    db = _db_returning_settings(
        OrganizationSettings(config={"slack_announce_new_report": bad_value})
    )
    assert await m._get_announce_new_report(db, "org1", "slack") is True


# --- message flow: send vs suppress ------------------------------------------


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


async def _run_flow(m, adapter, platform, channel_type, announce):
    fresh = SimpleNamespace(id="R1", title="Chat with Alice")
    with patch.object(
        m, "_get_announce_new_report", new=AsyncMock(return_value=announce)
    ), patch.object(
        m, "_get_session_max_age_hours", new=AsyncMock(return_value=24)
    ), patch.object(
        m, "_get_or_create_conversation_report", new=AsyncMock(return_value=(fresh, True))
    ), patch.object(
        m, "_find_recent_platform_report", new=AsyncMock(return_value=None)
    ):
        return await m._process_verified_message(
            _plain_db(), adapter, _data(platform, channel_type), _mapping(platform)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("platform,channel_type", [("slack", "im"), ("teams", "personal")])
async def test_announcement_sent_when_enabled(platform, channel_type):
    m = _manager()
    adapter = _adapter()
    res = await _run_flow(m, adapter, platform, channel_type, announce=True)
    assert res["action"] == "message_processed"
    assert adapter.send_dm_in_thread.await_count == 1
    assert "new conversation report" in adapter.send_dm_in_thread.await_args.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("platform,channel_type", [("slack", "im"), ("teams", "personal")])
async def test_announcement_suppressed_when_disabled(platform, channel_type):
    m = _manager()
    adapter = _adapter()
    res = await _run_flow(m, adapter, platform, channel_type, announce=False)
    assert res["action"] == "message_processed"
    assert adapter.send_dm_in_thread.await_count == 0


@pytest.mark.asyncio
async def test_whatsapp_never_announces_regardless_of_setting():
    m = _manager()
    adapter = _adapter()
    res = await _run_flow(m, adapter, "whatsapp", "im", announce=True)
    assert res["action"] == "message_processed"
    assert adapter.send_dm_in_thread.await_count == 0


# --- update-side validation ---------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ANNOUNCE_KEYS)
@pytest.mark.parametrize("bad_value", [0, 1, "true", None, 3.5])
async def test_update_rejects_non_bool(key, bad_value):
    from app.schemas.organization_settings_schema import OrganizationSettingsUpdate

    service = OrganizationSettingsService()
    settings_row = OrganizationSettings(config={key: True})

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
async def test_update_accepts_bool():
    from app.schemas.organization_settings_schema import OrganizationSettingsUpdate

    service = OrganizationSettingsService()
    settings_row = OrganizationSettings(config={"slack_announce_new_report": True})
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
            OrganizationSettingsUpdate(config={"slack_announce_new_report": False}),
        )

    assert result.config["slack_announce_new_report"] is False
    assert db.commit.await_count == 1


@pytest.mark.asyncio
async def test_update_normalizes_feature_shaped_payload():
    """A {'value': b} payload (ai_settings-style) is stored as the bare bool."""
    from app.schemas.organization_settings_schema import OrganizationSettingsUpdate

    service = OrganizationSettingsService()
    settings_row = OrganizationSettings(config={"teams_announce_new_report": True})
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
            OrganizationSettingsUpdate(config={"teams_announce_new_report": {"value": False}}),
        )

    assert result.config["teams_announce_new_report"] is False
