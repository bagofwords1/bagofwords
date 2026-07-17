"""Unit tests for the background eval-loop tool surface.

The full loop (run_eval background → watcher evaluates → wake fires →
get_eval_run reads) is exercised live in the sandbox feedback loop
(docs/feedback-loops/eval-agent-loop.md). Here we cover the deterministic
pieces:
  - run_eval input: wait_s bounds + case/suite exclusivity (regression)
  - detached output shape (_detached_output)
  - get_eval_runs / get_eval_run / edit_eval / cancel_wait / stop_eval_run
    input validation
  - cancel_wait behavior against a mocked WaitService
  - eval-tool digests for the new tools
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.ai.tools.schemas.run_eval import RunEvalInput, RunEvalOutput
from app.ai.tools.schemas.get_eval_run import GetEvalRunInput, GetEvalRunsInput
from app.ai.tools.schemas.edit_eval import EditEvalInput
from app.ai.tools.schemas.cancel_wait import CancelWaitInput
from app.ai.tools.schemas.stop_eval_run import StopEvalRunInput
from app.ai.tools.implementations.run_eval import RunEvalTool
from app.ai.tools.implementations.cancel_wait import CancelWaitTool


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


# --- run_eval input ----------------------------------------------------------


def test_run_eval_wait_s_defaults_to_background():
    inp = RunEvalInput(case_ids=["a"])
    assert inp.wait_s == 0


@pytest.mark.parametrize("wait_s,ok", [(0, True), (120, True), (600, True), (-1, False), (601, False)])
def test_run_eval_wait_s_bounds(wait_s, ok):
    if ok:
        assert RunEvalInput(case_ids=["a"], wait_s=wait_s).wait_s == wait_s
    else:
        with pytest.raises(Exception):
            RunEvalInput(case_ids=["a"], wait_s=wait_s)


def test_run_eval_input_exclusivity_still_enforced():
    with pytest.raises(Exception):
        RunEvalInput(case_ids=["a"], suite_id="s")
    with pytest.raises(Exception):
        RunEvalInput()


def test_detached_output_shape():
    out = RunEvalTool._detached_output(
        run_id="run-1",
        total=2,
        target_cases_meta={"c1": "Case one", "c2": "Case two"},
        target_case_ids=["c1", "c2"],
        deduped=False,
    )
    assert isinstance(out, RunEvalOutput)
    assert out.success is True
    assert out.detached is True
    assert out.status == "in_progress"
    assert out.run_id == "run-1"
    assert [r.status for r in out.results] == ["in_progress", "in_progress"]
    assert "get_eval_run" in (out.message or "")


def test_detached_output_deduped_message():
    out = RunEvalTool._detached_output(
        run_id="run-1", total=1,
        target_cases_meta={"c1": "Case"}, target_case_ids=["c1"], deduped=True,
    )
    assert out.deduped is True
    assert "already-running" in (out.message or "")


# --- read/edit/stop/cancel input schemas -------------------------------------


def test_get_eval_runs_input_defaults():
    inp = GetEvalRunsInput()
    assert inp.status == "all"
    assert inp.limit == 10
    with pytest.raises(Exception):
        GetEvalRunsInput(status="bogus")
    with pytest.raises(Exception):
        GetEvalRunsInput(limit=0)


def test_get_eval_run_input_requires_run_id():
    with pytest.raises(Exception):
        GetEvalRunInput()
    inp = GetEvalRunInput(run_id="r1")
    assert inp.compare_to_previous is False


def test_edit_eval_requires_a_change():
    with pytest.raises(Exception):
        EditEvalInput(case_id="c1")
    assert EditEvalInput(case_id="c1", status="active").status == "active"
    # Explicit empty tag list is a valid change (clears tags).
    assert EditEvalInput(case_id="c1", tags=[]).tags == []


def test_stop_eval_run_requires_run_id():
    with pytest.raises(Exception):
        StopEvalRunInput()
    assert StopEvalRunInput(run_id="r1").run_id == "r1"


def test_cancel_wait_input_job_id_optional():
    assert CancelWaitInput().job_id is None
    assert CancelWaitInput(job_id="wait:r:1").job_id == "wait:r:1"


# --- cancel_wait behavior -----------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_wait_requires_report_context():
    tool = CancelWaitTool()
    events = await _collect(tool, {}, _ctx(report=None))
    payload = _end_payload(events)
    assert payload["output"]["success"] is False


@pytest.mark.asyncio
async def test_cancel_wait_cancels_all_for_report():
    tool = CancelWaitTool()
    pending = [
        {"job_id": "wait:report-1:aaa", "wake_at": "2030-01-01T00:00:00+00:00", "reason": "check run"},
        {"job_id": "wait:report-1:bbb", "wake_at": "2030-01-01T01:00:00+00:00", "reason": "other"},
    ]
    db = MagicMock()

    async def _no_rows(*a, **k):
        res = MagicMock()
        res.scalars.return_value.all.return_value = []
        return res

    db.execute.side_effect = _no_rows
    with patch("app.services.wait_service.wait_service.list_waits", return_value=pending), \
         patch("app.services.wait_service.wait_service.cancel_wait", return_value=True) as mock_cancel:
        events = await _collect(tool, {}, _ctx(db=db))
    out = _end_payload(events)["output"]
    assert out["success"] is True
    assert {c["job_id"] for c in out["cancelled"]} == {"wait:report-1:aaa", "wait:report-1:bbb"}
    assert mock_cancel.call_count == 2


@pytest.mark.asyncio
async def test_cancel_wait_specific_job_only():
    tool = CancelWaitTool()
    pending = [
        {"job_id": "wait:report-1:aaa", "wake_at": None, "reason": None},
        {"job_id": "wait:report-1:bbb", "wake_at": None, "reason": None},
    ]
    db = MagicMock()

    async def _no_rows(*a, **k):
        res = MagicMock()
        res.scalars.return_value.all.return_value = []
        return res

    db.execute.side_effect = _no_rows
    with patch("app.services.wait_service.wait_service.list_waits", return_value=pending), \
         patch("app.services.wait_service.wait_service.cancel_wait", return_value=True) as mock_cancel:
        events = await _collect(tool, {"job_id": "wait:report-1:bbb"}, _ctx(db=db))
    out = _end_payload(events)["output"]
    assert [c["job_id"] for c in out["cancelled"]] == ["wait:report-1:bbb"]
    mock_cancel.assert_called_once_with("wait:report-1:bbb")


@pytest.mark.asyncio
async def test_cancel_wait_nothing_pending_is_success():
    tool = CancelWaitTool()
    with patch("app.services.wait_service.wait_service.list_waits", return_value=[]):
        events = await _collect(tool, {}, _ctx())
    out = _end_payload(events)["output"]
    assert out["success"] is True
    assert out["cancelled"] == []


# --- digests ------------------------------------------------------------------


def _te(name, output):
    return SimpleNamespace(tool_name=name, result_json={"output": output})


def test_digest_run_eval_detached():
    from app.ai.context.builders.message_context_builder import _digest_eval_tool

    d = _digest_eval_tool(_te("run_eval", {
        "run_id": "r1", "status": "in_progress", "detached": True,
        "passed": 0, "failed": 0, "total": 3, "results": [],
    }))
    assert "r1" in d and "detached" in d


def test_digest_get_eval_runs():
    from app.ai.context.builders.message_context_builder import _digest_eval_tool

    d = _digest_eval_tool(_te("get_eval_runs", {
        "items": [
            {"run_id": "r1", "status": "success", "passed": 2, "total": 2},
            {"run_id": "r2", "status": "in_progress", "passed": 0, "total": 1},
        ],
    }))
    assert "r1:success(2/2 pass)" in d and "r2:in_progress" in d


def test_digest_get_eval_run_with_compare():
    from app.ai.context.builders.message_context_builder import _digest_eval_tool

    d = _digest_eval_tool(_te("get_eval_run", {
        "run_id": "r1", "status": "success", "passed": 2, "failed": 0, "total": 2,
        "results": [], "compare": {"summary": {"fixed": 1, "regressed": 0}},
    }))
    assert "1 fixed" in d and "0 regressed" in d


def test_digest_edit_and_stop():
    from app.ai.context.builders.message_context_builder import _digest_eval_tool

    d = _digest_eval_tool(_te("edit_eval", {
        "success": True, "case_id": "c1", "name": "N", "status": "active",
        "changed_fields": ["status"],
    }))
    assert "changed: status" in d
    d2 = _digest_eval_tool(_te("stop_eval_run", {"success": True, "run_id": "r1", "status": "stopped"}))
    assert "stopped" in d2
