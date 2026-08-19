"""ToolRunner timeout invariants."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from app.ai.runner.policies import RetryPolicy, TimeoutPolicy
from app.ai.runner.tool_runner import ToolRunner


class _EndlessHealthyTool:
    name = "endless_healthy_tool"
    spec = None
    metadata = None
    input_model = None
    output_model = None

    async def run_stream(self, tool_input, runtime_ctx) -> AsyncIterator[dict]:
        yield {"type": "tool.progress", "payload": {"stage": "working"}}
        while True:
            await asyncio.sleep(0.005)
            yield {
                "type": "tool.progress",
                "payload": {
                    "stage": "working",
                    "heartbeat": True,
                    "timing": False,
                },
            }


@pytest.mark.asyncio
async def test_heartbeats_do_not_bypass_hard_timeout():
    runner = ToolRunner(
        retry=RetryPolicy(max_attempts=1),
        timeout=TimeoutPolicy(
            start_timeout_s=0.01,
            idle_timeout_s=0.05,
            hard_timeout_s=0.06,
        ),
    )

    async def emit(event):
        return None

    result = await asyncio.wait_for(
        runner.run(_EndlessHealthyTool(), {}, {"mode": "chat"}, emit),
        timeout=0.25,
    )

    assert result["success"] is False
    assert result["error"]["type"] == "timeout_error"
    assert result["error"]["message"] == "hard timeout"
    assert result["retry_exhausted"] is True
    assert "analysis_complete" not in result
