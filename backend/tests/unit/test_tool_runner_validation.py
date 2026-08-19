"""ToolRunner input-validation failure handling.

Regressions from the write_csv "user_prompt: Field required" incident:

1. The failure counter accumulated isolated malformed calls across a long run.
   A streak must be the exact same call in adjacent planner rounds.
2. Validation failures returned a bare observation with no ``output`` half,
   so the tool_execution's result_json carried no ``success: False`` and the
   UI rendered the failed call as a completed tool ("CSV written ✓").
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from pydantic import BaseModel, Field

from app.ai.runner.tool_runner import ToolRunner


class _EchoInput(BaseModel):
    user_prompt: str = Field(...)


class _FakeTool:
    name = "fake_tool"
    spec = None
    metadata = None

    @property
    def input_model(self) -> type[BaseModel]:
        return _EchoInput

    output_model = None

    async def run_stream(self, tool_input, runtime_ctx) -> AsyncIterator[dict]:
        yield {
            "type": "tool.end",
            "payload": {
                "output": {"echo": tool_input},
                "observation": {"summary": "ok", "success": True},
            },
        }


async def _noop_emit(event):
    return None


async def _run(runner, arguments, runtime_ctx=None):
    return await runner.run(
        _FakeTool(),
        arguments,
        {"mode": "chat", **(runtime_ctx or {})},
        _noop_emit,
    )


@pytest.mark.asyncio
async def test_validation_failure_reports_failed_output():
    runner = ToolRunner()
    result = await _run(runner, {})

    observation = result["observation"]
    assert observation["success"] is False
    assert observation["error"]["type"] == "validation_error"
    assert "analysis_complete" not in observation

    output = result["output"]
    assert output["success"] is False
    assert "user_prompt" in output["error_message"]


@pytest.mark.asyncio
async def test_repeated_consecutive_failures_request_strategy_change_without_terminating():
    runner = ToolRunner()
    await _run(runner, {})
    await _run(runner, {})
    result = await _run(runner, {})

    observation = result["observation"]
    assert observation["error"]["type"] == "repeated_validation_error"
    assert observation["retry_exhausted"] is True
    assert observation["suggested_action"] == "change_strategy"
    assert "analysis_complete" not in observation
    assert "final_answer" not in observation
    assert result["output"]["success"] is False


@pytest.mark.asyncio
async def test_valid_call_resets_failure_streak():
    runner = ToolRunner()
    await _run(runner, {})  # strike 1
    await _run(runner, {"user_prompt": "ok"})  # valid call clears the streak
    result = await _run(runner, {})  # counts as strike 1 again, not strike 2

    assert result["observation"]["error"]["type"] == "validation_error"
    assert "analysis_complete" not in result["observation"]


@pytest.mark.asyncio
async def test_distant_same_validation_failure_starts_a_new_streak():
    runner = ToolRunner()
    await _run(runner, {}, {"planner_round_index": 1})
    result = await _run(runner, {}, {"planner_round_index": 55})

    assert result["observation"]["error"]["type"] == "validation_error"
    assert "retry_exhausted" not in result["observation"]


@pytest.mark.asyncio
async def test_changed_invalid_arguments_are_a_different_approach():
    runner = ToolRunner()
    await _run(runner, {})
    await _run(runner, {"unexpected": "first"})
    result = await _run(runner, {"unexpected": "second"})

    assert result["observation"]["error"]["type"] == "validation_error"
    assert "retry_exhausted" not in result["observation"]
