"""A tool failure is retried by the runner up to its policy's attempts.

Contract: an exception a tool raises (one no tool reports as its own
observation) is retried; when every attempt failed the result says the
retries ran out, and the run is not ended by it.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from app.ai.runner.policies import RetryPolicy
from app.ai.runner.tool_runner import ToolRunner


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


@pytest.mark.asyncio
async def test_an_ordinary_failure_is_still_retried():
    tool = _FailingTool(RuntimeError("temporary"))

    async def emit(_event):
        return None

    runner = ToolRunner(retry=RetryPolicy(max_attempts=3, backoff_ms=1, jitter_ms=0))
    result = await runner.run(tool, {}, {"mode": "chat"}, emit)

    assert tool.calls == 3
    assert result["retry_exhausted"] is True
    assert "analysis_complete" not in result
    assert "terminal_execution_error" not in result
