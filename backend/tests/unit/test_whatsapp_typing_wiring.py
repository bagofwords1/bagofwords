"""The WhatsApp "typing…" indicator is wired into the real message flow.

test_whatsapp_typing_indicator covers the indicator's own timing; these
tests drive the pieces that turn it on and off in production — the platform
manager handling an inbound WhatsApp message, and the completion status
listener reporting a run's end — and check what the indicator does, through
an adapter that records each typing request.
"""
import asyncio
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm.attributes import set_committed_value

from app.models.completion import Completion, after_update_completion
from app.services.external_platform_manager import ExternalPlatformManager
from app.services.platform_adapters import whatsapp_typing

PNID = "PNID_1"
WA_ID = "15550001111"


@pytest.fixture(autouse=True)
def _isolated_typing_state(monkeypatch):
    for name in ("_chats", "_runs", "_bound"):
        monkeypatch.setattr(whatsapp_typing, name, {})
    for name in ("_ended_ids", "_early_ends"):
        monkeypatch.setattr(whatsapp_typing, name, OrderedDict())
    yield
    for chat in whatsapp_typing._chats.values():
        if chat.task is not None:
            chat.task.cancel()


@pytest.fixture(autouse=True)
def _principal_belongs_to_org():
    with patch("app.core.permission_resolver.principal_belongs_to_org", new=AsyncMock(return_value=True)), \
         patch("app.core.workspace_access.has_workspace_access", new=AsyncMock(return_value=True)):
        yield


class RecordingAdapter:
    """A WhatsApp adapter as the manager uses it, recording typing requests."""

    def __init__(self):
        self.typing_for = []
        self.add_reaction = AsyncMock()
        self.send_dm_in_thread = AsyncMock()

    def phone_number_id(self):
        return PNID

    async def send_typing_indicator(self, message_id):
        self.typing_for.append(message_id)
        return whatsapp_typing.SENT


def _manager(create_completion):
    m = ExternalPlatformManager()
    m.mapping_service.get_user_by_id = AsyncMock(return_value=SimpleNamespace(id="u1", name="Alice"))
    m.organization_service.get_organization = AsyncMock(return_value=SimpleNamespace(id="org1"))
    m.completion_service.create_completion = create_completion
    return m


def _db():
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=None)
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


async def _message(m, adapter, message_id="wamid.Q1"):
    data = {
        "platform_type": "whatsapp", "message_text": "show me sales", "thread_ts": message_id,
        "message_ts": message_id, "channel_id": WA_ID, "channel_type": "im", "is_thread_reply": False,
    }
    mapping = SimpleNamespace(app_user_id="u1", organization_id="org1", external_user_id=WA_ID,
                              platform_type="whatsapp", platform_id="plat-1")
    report = SimpleNamespace(id="R1", title="Chat with Alice")
    with patch.object(m, "_get_session_max_age_hours", new=AsyncMock(return_value=24)), \
            patch.object(m, "_get_or_create_conversation_report", new=AsyncMock(return_value=(report, True))):
        return await m._process_verified_message(_db(), adapter, data, mapping)


def _created(system_id):
    return SimpleNamespace(completions=[
        SimpleNamespace(id="head-1", role="user"),
        SimpleNamespace(id=system_id, role="system"),
    ])


def _run_ended(completion_id, status="success"):
    """What the completion status listener sees when a run's status flips."""
    target = Completion(id=completion_id, role="system", external_platform="whatsapp",
                        external_channel_id=WA_ID, external_user_id=None)
    set_committed_value(target, "status", "in_progress")
    target.status = status
    after_update_completion(None, None, target)


def _typing_is_on():
    return any(c.runs for c in whatsapp_typing._chats.values())


async def _settle():
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["success", "error", "stopped", "cancelled"])
async def test_a_whatsapp_question_shows_typing_until_its_run_ends(status):
    adapter = RecordingAdapter()
    m = _manager(AsyncMock(return_value=_created("sys-1")))

    res = await _message(m, adapter)
    await _settle()
    assert res["action"] == "message_processed"
    assert adapter.typing_for == ["wamid.Q1"]
    assert _typing_is_on()

    _run_ended("sys-1", status)
    assert not _typing_is_on()


@pytest.mark.asyncio
async def test_another_rows_end_does_not_stop_this_runs_typing():
    """e.g. a stale row from before a restart retired while this run starts."""
    adapter = RecordingAdapter()
    m = _manager(AsyncMock(return_value=_created("sys-live")))

    await _message(m, adapter)
    _run_ended("sys-stale", "error")
    assert _typing_is_on()

    _run_ended("sys-live")
    assert not _typing_is_on()


@pytest.mark.asyncio
async def test_a_failed_run_creation_leaves_no_typing():
    adapter = RecordingAdapter()
    m = _manager(AsyncMock(side_effect=RuntimeError("db down")))

    with pytest.raises(RuntimeError):
        await _message(m, adapter)
    assert not _typing_is_on()


@pytest.mark.asyncio
async def test_a_queued_question_keeps_typing_until_its_own_run_ends():
    adapter = RecordingAdapter()
    m = _manager(AsyncMock(side_effect=[_created("sys-1"), _created("sys-2")]))

    await _message(m, adapter, "wamid.Q1")
    await _message(m, adapter, "wamid.Q2")
    _run_ended("sys-1")
    assert _typing_is_on()

    _run_ended("sys-2")
    assert not _typing_is_on()


@pytest.fixture(autouse=True)
def _member_has_workspace_access(monkeypatch):
    """These tests drive a member's chat message on a mocked database, which
    can't resolve permissions; the app-user gate (#1914) is covered by its own
    tests, so a member here has the workspace."""
    monkeypatch.setattr("app.core.workspace_access.has_workspace_access",
                        AsyncMock(return_value=True))
