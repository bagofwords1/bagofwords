"""Unit tests for the create_scheduled_task / cancel_scheduled_task tools.

The full agent loop (and real DB persistence) is exercised by the Haiku eval
suite (tests/evals/suites/sanity_scheduled_task.yaml). Here we cover the tool
logic deterministically, with the ScheduledPromptService mocked:
 - input validation
 - the 1-hour cron floor (single-number minute field)
 - missing report/user context guard
 - create happy path -> success output + observation
 - cancel: not-found / wrong-report guard, and happy path
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.tools.implementations.create_scheduled_task import (
    CreateScheduledTaskTool,
    _minute_field_is_single_value,
)
from app.ai.tools.implementations.cancel_scheduled_task import CancelScheduledTaskTool
from app.ai.tools.implementations.edit_scheduled_task import EditScheduledTaskTool
from app.ai.tools.schemas.create_scheduled_task import CreateScheduledTaskInput
from app.ai.tools.schemas.cancel_scheduled_task import CancelScheduledTaskInput
from app.ai.tools.schemas.edit_scheduled_task import EditScheduledTaskInput


def _ctx(**overrides) -> dict:
    ctx = {
        "db": MagicMock(),
        "user": SimpleNamespace(id="user-1", email="me@test.com"),
        "report": SimpleNamespace(id="report-1"),
        "organization": SimpleNamespace(id="org-1"),
    }
    ctx.update(overrides)
    return ctx


async def _collect(tool, tool_input, ctx):
    events = []
    async for evt in tool.run_stream(tool_input, ctx):
        events.append(evt)
    return events


def _end_payload(events):
    end = [e for e in events if e.type == "tool.end"]
    assert end, "expected a tool.end event"
    return end[-1].payload


def _error_events(events):
    return [e for e in events if e.type == "tool.error"]


# --- cron floor helper (pure) ----------------------------------------------


@pytest.mark.parametrize(
    "cron,expected",
    [
        ("0 9 * * 1", True),      # weekly Monday 9am
        ("0 8 * * *", True),      # daily 8am
        ("30 7 1 * *", True),     # 7:30 on the 1st
        ("0 * * * *", True),      # hourly (the floor)
        ("59 23 * * 5", True),
        ("* * * * *", False),     # every minute
        ("*/5 * * * *", False),   # every 5 minutes
        ("0,30 * * * *", False),  # twice an hour
        ("0-10 * * * *", False),  # range within the hour
        ("0 9 * * 1 *", False),   # 6-field (seconds) not allowed
        ("not a cron", False),
        ("60 0 * * *", False),    # minute out of range
    ],
)
def test_minute_field_floor(cron, expected):
    assert _minute_field_is_single_value(cron) is expected


# --- input validation -------------------------------------------------------


def test_create_input_requires_fields():
    with pytest.raises(Exception):
        CreateScheduledTaskInput()


def test_cancel_input_requires_task_id():
    with pytest.raises(Exception):
        CancelScheduledTaskInput()


def test_edit_input_requires_task_id():
    with pytest.raises(Exception):
        EditScheduledTaskInput()
    # task_id alone is valid (the no-op guard lives in the tool, not the schema).
    assert EditScheduledTaskInput(task_id="sp-1").task_id == "sp-1"


# --- create: context guard --------------------------------------------------


@pytest.mark.asyncio
async def test_create_requires_report_context():
    tool = CreateScheduledTaskTool()
    events = await _collect(
        tool,
        {"task_prompt": "do x", "cron_schedule": "0 9 * * 1"},
        _ctx(report=None),
    )
    out = _end_payload(events)["output"]
    assert out["success"] is False
    assert "report" in (out["error"] or "").lower()


# --- create: cron floor enforced -------------------------------------------


@pytest.mark.asyncio
async def test_create_rejects_subhourly_cron():
    tool = CreateScheduledTaskTool()
    with patch(
        "app.services.scheduled_prompt_service.scheduled_prompt_service.create_scheduled_prompt",
        new=AsyncMock(),
    ) as mock_create:
        events = await _collect(
            tool,
            {"task_prompt": "do x", "cron_schedule": "*/5 * * * *"},
            _ctx(),
        )
        mock_create.assert_not_called()
    out = _end_payload(events)["output"]
    assert out["success"] is False
    assert "hour" in (out["error"] or "").lower()


# --- create: happy path -----------------------------------------------------


@pytest.mark.asyncio
async def test_create_happy_path():
    tool = CreateScheduledTaskTool()
    fake_sp = SimpleNamespace(id="sp-123")
    with patch(
        "app.services.scheduled_prompt_service.scheduled_prompt_service.create_scheduled_prompt",
        new=AsyncMock(return_value=fake_sp),
    ) as mock_create:
        events = await _collect(
            tool,
            {
                "task_prompt": "Check for weird activity and email me a summary.",
                "cron_schedule": "0 9 * * 1",
            },
            _ctx(),
        )
        mock_create.assert_awaited_once()
        # The stored prompt is the task_prompt verbatim, no email subscribers.
        kwargs = mock_create.await_args.kwargs
        assert kwargs["report_id"] == "report-1"
        assert kwargs["data"].prompt == {"content": "Check for weird activity and email me a summary."}
        assert kwargs["data"].cron_schedule == "0 9 * * 1"
        assert kwargs["data"].notification_subscribers is None

    payload = _end_payload(events)
    assert payload["output"]["success"] is True
    assert payload["output"]["task_id"] == "sp-123"
    assert payload["output"]["cron_schedule"] == "0 9 * * 1"
    assert payload["observation"]["success"] is True


# --- cancel: guards ---------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_not_found():
    tool = CancelScheduledTaskTool()
    db = MagicMock()
    db.get = AsyncMock(return_value=None)
    with patch(
        "app.services.scheduled_prompt_service.scheduled_prompt_service.delete_scheduled_prompt",
        new=AsyncMock(),
    ) as mock_del:
        events = await _collect(tool, {"task_id": "missing"}, _ctx(db=db))
        mock_del.assert_not_called()
    out = _end_payload(events)["output"]
    assert out["success"] is False


@pytest.mark.asyncio
async def test_cancel_rejects_other_report_task():
    tool = CancelScheduledTaskTool()
    other = SimpleNamespace(id="sp-9", report_id="other-report", deleted_at=None)
    db = MagicMock()
    db.get = AsyncMock(return_value=other)
    with patch(
        "app.services.scheduled_prompt_service.scheduled_prompt_service.delete_scheduled_prompt",
        new=AsyncMock(),
    ) as mock_del:
        events = await _collect(tool, {"task_id": "sp-9"}, _ctx(db=db))
        mock_del.assert_not_called()
    out = _end_payload(events)["output"]
    assert out["success"] is False


# --- edit: guards -----------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_requires_report_context():
    tool = EditScheduledTaskTool()
    events = await _collect(
        tool,
        {"task_id": "sp-1", "is_active": False},
        _ctx(report=None),
    )
    out = _end_payload(events)["output"]
    assert out["success"] is False
    assert "report" in (out["error"] or "").lower()


@pytest.mark.asyncio
async def test_edit_requires_at_least_one_field():
    tool = EditScheduledTaskTool()
    with patch(
        "app.services.scheduled_prompt_service.scheduled_prompt_service.update_scheduled_prompt",
        new=AsyncMock(),
    ) as mock_update:
        events = await _collect(tool, {"task_id": "sp-1"}, _ctx())
        mock_update.assert_not_called()
    out = _end_payload(events)["output"]
    assert out["success"] is False
    assert "at least one" in (out["error"] or "").lower()


@pytest.mark.asyncio
async def test_edit_rejects_subhourly_cron():
    tool = EditScheduledTaskTool()
    with patch(
        "app.services.scheduled_prompt_service.scheduled_prompt_service.update_scheduled_prompt",
        new=AsyncMock(),
    ) as mock_update:
        events = await _collect(
            tool,
            {"task_id": "sp-1", "cron_schedule": "*/5 * * * *"},
            _ctx(),
        )
        mock_update.assert_not_called()
    out = _end_payload(events)["output"]
    assert out["success"] is False
    assert "hour" in (out["error"] or "").lower()


@pytest.mark.asyncio
async def test_edit_rejects_other_report_task():
    tool = EditScheduledTaskTool()
    other = SimpleNamespace(id="sp-9", report_id="other-report", deleted_at=None, prompt={"content": "x"})
    db = MagicMock()
    db.get = AsyncMock(return_value=other)
    with patch(
        "app.services.scheduled_prompt_service.scheduled_prompt_service.update_scheduled_prompt",
        new=AsyncMock(),
    ) as mock_update:
        events = await _collect(tool, {"task_id": "sp-9", "is_active": False}, _ctx(db=db))
        mock_update.assert_not_called()
    out = _end_payload(events)["output"]
    assert out["success"] is False


# --- edit: happy path -------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_happy_path_all_fields():
    tool = EditScheduledTaskTool()
    sp = SimpleNamespace(id="sp-1", report_id="report-1", deleted_at=None, prompt={"content": "old", "mode": "chat"})
    updated = SimpleNamespace(id="sp-1", cron_schedule="0 7 * * 5", is_active=True)
    db = MagicMock()
    db.get = AsyncMock(return_value=sp)
    with patch(
        "app.services.scheduled_prompt_service.scheduled_prompt_service.update_scheduled_prompt",
        new=AsyncMock(return_value=updated),
    ) as mock_update:
        events = await _collect(
            tool,
            {
                "task_id": "sp-1",
                "task_prompt": "new prompt",
                "cron_schedule": "0 7 * * 5",
                "is_active": True,
            },
            _ctx(db=db),
        )
        mock_update.assert_awaited_once()
        kwargs = mock_update.await_args.kwargs
        # The prompt patch preserves other prompt fields and swaps only content.
        assert kwargs["data"].prompt == {"content": "new prompt", "mode": "chat"}
        assert kwargs["data"].cron_schedule == "0 7 * * 5"
        assert kwargs["data"].is_active is True

    payload = _end_payload(events)
    assert payload["output"]["success"] is True
    assert payload["output"]["task_id"] == "sp-1"
    assert payload["output"]["cron_schedule"] == "0 7 * * 5"
    assert payload["output"]["is_active"] is True
    assert payload["observation"]["success"] is True


@pytest.mark.asyncio
async def test_edit_prompt_only_leaves_other_fields_unset():
    tool = EditScheduledTaskTool()
    sp = SimpleNamespace(id="sp-2", report_id="report-1", deleted_at=None, prompt={"content": "old"})
    updated = SimpleNamespace(id="sp-2", cron_schedule="0 9 * * 1", is_active=True)
    db = MagicMock()
    db.get = AsyncMock(return_value=sp)
    with patch(
        "app.services.scheduled_prompt_service.scheduled_prompt_service.update_scheduled_prompt",
        new=AsyncMock(return_value=updated),
    ) as mock_update:
        events = await _collect(tool, {"task_id": "sp-2", "task_prompt": "just the prompt"}, _ctx(db=db))
        kwargs = mock_update.await_args.kwargs
        assert kwargs["data"].prompt == {"content": "just the prompt"}
        # Omitted fields stay None so the service leaves them unchanged.
        assert kwargs["data"].cron_schedule is None
        assert kwargs["data"].is_active is None
    assert _end_payload(events)["output"]["success"] is True


# --- conversation-history digest -------------------------------------------


def test_digest_scheduled_tool():
    from types import SimpleNamespace
    from app.ai.context.builders.message_context_builder import _digest_scheduled_tool

    created = SimpleNamespace(
        tool_name="create_scheduled_task",
        result_json={"success": True, "task_id": "sp-1", "cron_schedule": "0 9 * * 0"},
    )
    d = _digest_scheduled_tool(created)
    assert "task_id: sp-1" in d and "cron: 0 9 * * 0" in d

    cancelled = SimpleNamespace(
        tool_name="cancel_scheduled_task",
        result_json={"success": True, "task_id": "sp-1"},
    )
    assert "task_id: sp-1" in _digest_scheduled_tool(cancelled)

    edited = SimpleNamespace(
        tool_name="edit_scheduled_task",
        result_json={"success": True, "task_id": "sp-1", "cron_schedule": "0 7 * * 5", "is_active": False},
    )
    de = _digest_scheduled_tool(edited)
    assert "task_id: sp-1" in de and "cron: 0 7 * * 5" in de and "active: False" in de

    # Non-scheduled tool falls through (empty -> caller tries next digest).
    other = SimpleNamespace(tool_name="create_data", result_json={"x": 1})
    assert _digest_scheduled_tool(other) == ""


@pytest.mark.asyncio
async def test_cancel_happy_path():
    tool = CancelScheduledTaskTool()
    sp = SimpleNamespace(id="sp-1", report_id="report-1", deleted_at=None)
    db = MagicMock()
    db.get = AsyncMock(return_value=sp)
    with patch(
        "app.services.scheduled_prompt_service.scheduled_prompt_service.delete_scheduled_prompt",
        new=AsyncMock(),
    ) as mock_del:
        events = await _collect(tool, {"task_id": "sp-1"}, _ctx(db=db))
        mock_del.assert_awaited_once()
    payload = _end_payload(events)
    assert payload["output"]["success"] is True
    assert payload["output"]["task_id"] == "sp-1"
    assert payload["observation"]["success"] is True
