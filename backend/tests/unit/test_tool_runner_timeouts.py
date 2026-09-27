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


class _DeadlineRecordingTool:
    """Fails its first attempt; records the deadline each attempt sees."""
    name = "deadline_recording_tool"
    spec = None
    metadata = None
    input_model = None
    output_model = None

    def __init__(self):
        self.seen = []

    async def run_stream(self, tool_input, runtime_ctx) -> AsyncIterator[dict]:
        import time
        self.seen.append((runtime_ctx.get("tool_deadline_monotonic"), time.monotonic()))
        yield {"type": "tool.progress", "payload": {"stage": "working"}}
        if len(self.seen) == 1:
            raise RuntimeError("transient")
        yield {"type": "tool.end", "payload": {"observation": {"summary": "ok"}, "output": {"ok": True}}}


@pytest.mark.asyncio
async def test_runtime_ctx_carries_one_absolute_deadline_across_attempts():
    import time
    runner = ToolRunner(
        retry=RetryPolicy(max_attempts=2, backoff_ms=1, jitter_ms=0),
        timeout=TimeoutPolicy(start_timeout_s=5, idle_timeout_s=10, hard_timeout_s=30),
    )
    tool = _DeadlineRecordingTool()

    async def emit(event):
        return None

    started = time.monotonic()
    await runner.run(tool, {}, {"mode": "chat"}, emit)

    assert len(tool.seen) == 2, "the first attempt fails and is retried"
    (d1, _), (d2, t2) = tool.seen
    assert d1 is not None and d1 == d2, "every attempt shares the run's deadline"
    # hard budget = max(hard 30, start 5 + idle 10) = 30 s from the run start
    assert started + 30 - 0.5 <= d1 <= started + 30 + 0.5
    assert d2 - t2 < 30, "a retry sees less than a fresh budget"
