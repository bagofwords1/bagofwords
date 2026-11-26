"""A tool failure that no retry can repair ends the run at once.

Contract: an exception that carries `terminal_execution_error` (the caller's
transaction is gone, the sandbox cannot run) is not retried by the runner.
It comes back as a terminal observation that completes the analysis with
the error's own fixed message, as create_data reports such a failure.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from app.ai.runner.policies import RetryPolicy
from app.ai.runner.tool_runner import ToolRunner
from app.services.entity_service import CallerTransactionLost


class _FailingTool:
    name = "failing_tool"
    spec = None
    metadata = None
    input_model = None
    output_model = None

    def __init__(self, error: Exception):
        self.error = error
        self.calls = 0

    async def run_stream(self, tool_input, runtime_ctx) -> AsyncIterator[dict]:
        self.calls += 1
        yield {"type": "tool.progress", "payload": {"stage": "working"}}
        raise self.error


async def _run(tool):
    events = []

    async def emit(event):
        events.append(event)

    runner = ToolRunner(retry=RetryPolicy(max_attempts=3, backoff_ms=1, jitter_ms=0))
    return await runner.run(tool, {}, {"mode": "chat"}, emit), events


@pytest.mark.asyncio
async def test_a_lost_caller_transaction_ends_the_run_without_a_retry():
    tool = _FailingTool(CallerTransactionLost())
    result, events = await _run(tool)

    assert tool.calls == 1
    assert result["success"] is False
    assert result["terminal_execution_error"] is True
    assert result["analysis_complete"] is True
    assert result["final_answer"] == "The run's result could not be saved."
    assert result["error"]["message"] == "The run's result could not be saved."
    assert [e["type"] for e in events].count("tool.error") == 1


@pytest.mark.asyncio
async def test_an_ordinary_failure_is_still_retried():
    tool = _FailingTool(RuntimeError("temporary"))
    result, _events = await _run(tool)

    assert tool.calls == 3
    assert result["retry_exhausted"] is True
    assert "analysis_complete" not in result
    assert "terminal_execution_error" not in result
