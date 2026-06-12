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
from app.ai.tools.schemas.create_scheduled_task import CreateScheduledTaskInput
from app.ai.tools.schemas.cancel_scheduled_task import CancelScheduledTaskInput


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
        # The stored prompt is the task_prompt verbatim. This prompt asks to
        # "email me a summary", so the agent's send_email tool handles delivery
        # and we attach NO static summary subscribers (avoids double-sending).
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


@pytest.mark.asyncio
async def test_create_attaches_summary_email_when_no_intent():
    """A prompt with no email intent gets a static summary email to the creator."""
    tool = CreateScheduledTaskTool()
    fake_sp = SimpleNamespace(id="sp-456")
    with patch(
        "app.services.scheduled_prompt_service.scheduled_prompt_service.create_scheduled_prompt",
        new=AsyncMock(return_value=fake_sp),
    ) as mock_create:
        events = await _collect(
            tool,
            {
                "task_prompt": "Refresh the revenue dashboard with the latest data.",
                "cron_schedule": "0 7 * * *",
            },
            _ctx(),
        )
        mock_create.assert_awaited_once()
        subs = mock_create.await_args.kwargs["data"].notification_subscribers
        assert subs is not None and len(subs) == 1
        assert subs[0].type == "user"
        assert subs[0].id == "user-1"

    assert _end_payload(events)["output"]["success"] is True


@pytest.mark.parametrize(
    "prompt,expected",
    [
        ("Check activity this week and email me a short summary", True),
        ("Pull yesterday's signups and email me the count", True),
        ("Notify me if revenue drops below target", True),
        ("Let me know if anything looks off", True),
        ("Ping me when the sync finishes", True),
        ("Refresh the revenue dashboard with the latest data.", False),
        ("Run the daily report", False),
        ("Send the data to the warehouse", False),
        ("Summarize the emails in the support queue", False),
        ("", False),
    ],
)
def test_prompt_requests_email(prompt, expected):
    from app.ai.tools.utils import prompt_requests_email

    assert prompt_requests_email(prompt) is expected


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
