"""Unit tests for the wait tool + WaitService.

The full agent loop (create_data -> wait -> auto-resume) is exercised live in the
sandbox feedback loop (docs/feedback-loops/wait-tool.md). Here we cover the tool
and service logic deterministically:
  - input validation + bounds (1..1440 minutes)
  - missing report/user context guard
  - happy path -> scheduled output + TERMINAL observation (analysis_complete)
  - the delay-phrasing helper
  - WaitService arms a one-shot 'date' job and cancels it
  - the job callable is a module-level function (survives jobstore serialization)
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.ai.tools.implementations.wait import WaitTool, _format_delay
from app.ai.tools.schemas.wait import WaitInput, MIN_WAIT_MINUTES, MAX_WAIT_MINUTES


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


# --- input validation + bounds ---------------------------------------------


def test_input_requires_fields():
    with pytest.raises(Exception):
        WaitInput()
    with pytest.raises(Exception):
        WaitInput(delay_minutes=5)  # reason missing
    with pytest.raises(Exception):
        WaitInput(reason="x")       # delay missing


@pytest.mark.parametrize("minutes,ok", [
    (MIN_WAIT_MINUTES, True),
    (30, True),
    (MAX_WAIT_MINUTES, True),
    (0, False),                 # below floor
    (MAX_WAIT_MINUTES + 1, False),  # above ceiling
])
def test_delay_bounds(minutes, ok):
    if ok:
        assert WaitInput(delay_minutes=minutes, reason="r").delay_minutes == minutes
    else:
        with pytest.raises(Exception):
            WaitInput(delay_minutes=minutes, reason="r")


# --- delay phrasing ---------------------------------------------------------


@pytest.mark.parametrize("minutes,phrase", [
    (1, "1 minute"),
    (30, "30 minutes"),
    (60, "1 hour"),
    (120, "2 hours"),
    (90, "1 hour 30 minutes"),
])
def test_format_delay(minutes, phrase):
    assert _format_delay(minutes) == phrase


# --- context guard ----------------------------------------------------------


@pytest.mark.asyncio
async def test_requires_report_context():
    tool = WaitTool()
    with patch("app.services.wait_service.wait_service.schedule_wait") as mock_sched:
        events = await _collect(tool, {"delay_minutes": 30, "reason": "retry"}, _ctx(report=None))
        mock_sched.assert_not_called()
    obs = _end_payload(events)["observation"]
    assert obs["success"] is False
    assert "report" in obs["summary"].lower()


# --- happy path: arms a wait, ends the turn ---------------------------------


@pytest.mark.asyncio
async def test_happy_path_arms_and_terminates():
    tool = WaitTool()
    armed = {"job_id": "wait:report-1:abc123", "wake_at": "2030-01-01T00:30:00+00:00"}
    with patch(
        "app.services.wait_service.wait_service.schedule_wait",
        return_value=armed,
    ) as mock_sched:
        events = await _collect(
            tool,
            {"delay_minutes": 30, "reason": "Re-run the export; table was refreshing."},
            _ctx(),
        )
        mock_sched.assert_called_once()
        kwargs = mock_sched.call_args.kwargs
        assert kwargs["report_id"] == "report-1"
        assert kwargs["user_id"] == "user-1"
        assert kwargs["organization_id"] == "org-1"
        assert kwargs["delay_minutes"] == 30
        assert "export" in kwargs["reason"]

    payload = _end_payload(events)
    out = payload["output"]
    assert out["status"] == "scheduled"
    assert out["job_id"] == armed["job_id"]
    assert out["wake_at"] == armed["wake_at"]
    assert out["delay_minutes"] == 30

    obs = payload["observation"]
    # The turn must END on a wait (same terminal contract as clarify).
    assert obs["analysis_complete"] is True
    assert obs["success"] is True
    assert "30 minutes" in obs["final_answer"]


# --- WaitService: arm + cancel against a stubbed scheduler ------------------


@pytest.mark.asyncio
async def test_service_arms_one_shot_date_job():
    from app.services import wait_service as ws

    with patch.object(ws, "scheduler") as sched:
        res = ws.wait_service.schedule_wait(
            report_id="report-1", user_id="u", organization_id="o",
            reason="retry later", delay_minutes=30,
        )
        sched.add_job.assert_called_once()
        kwargs = sched.add_job.call_args.kwargs
        # One-shot 'date' trigger — NOT a recurring cron.
        assert kwargs["trigger"] == "date"
        assert kwargs["func"] is ws.run_wait_wake
        assert kwargs["id"] == res["job_id"]
        assert res["job_id"].startswith("wait:report-1:")
        assert kwargs["kwargs"]["reason"] == "retry later"


def test_service_cancel_removes_job():
    from app.services import wait_service as ws

    with patch.object(ws, "scheduler") as sched:
        ok = ws.wait_service.cancel_wait("wait:report-1:abc")
        sched.remove_job.assert_called_once_with(job_id="wait:report-1:abc")
        assert ok is True

    # A non-wait id is refused without touching the scheduler.
    with patch.object(ws, "scheduler") as sched:
        assert ws.wait_service.cancel_wait("sched:whatever") is False
        sched.remove_job.assert_not_called()


def test_wake_callable_is_module_level_and_serializable():
    """The APScheduler job func must be a module-level function so it survives
    SQLAlchemyJobStore serialization (a bound method loses `self` on reload)."""
    from apscheduler.util import obj_to_ref, ref_to_obj
    from app.services.wait_service import run_wait_wake

    ref = obj_to_ref(run_wait_wake)
    assert ref == "app.services.wait_service:run_wait_wake"
    assert ref_to_obj(ref) is run_wait_wake
