"""ToolRunner input-validation failure handling.

Two regressions from the write_csv "user_prompt: Field required" incident:

1. The failure counter was never reset on a valid call, so two isolated
   malformed calls anywhere in a long run — even with successes in between —
   tripped the cap and killed the whole run with a forced final_answer.
2. Validation failures returned a bare observation with no ``output`` half,
   so the tool_execution's result_json carried no ``success: False`` and the
   UI rendered the failed call as a completed tool ("CSV written ✓").
"""
from __future__ import annotations

from typing import AsyncIterator, Type

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
    def input_model(self) -> Type[BaseModel]:
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


async def _run(runner, arguments):
    return await runner.run(_FakeTool(), arguments, {"mode": "chat"}, _noop_emit)


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
async def test_repeated_consecutive_failures_terminate():
    runner = ToolRunner()
    await _run(runner, {})
    result = await _run(runner, {})

    observation = result["observation"]
    assert observation["error"]["type"] == "repeated_validation_error"
    assert observation["analysis_complete"] is True
    assert "user_prompt" in observation["final_answer"]
    assert result["output"]["success"] is False


@pytest.mark.asyncio
async def test_valid_call_resets_failure_streak():
    runner = ToolRunner()
    await _run(runner, {})  # strike 1
    await _run(runner, {"user_prompt": "ok"})  # valid call clears the streak
    result = await _run(runner, {})  # counts as strike 1 again, not strike 2

    assert result["observation"]["error"]["type"] == "validation_error"
    assert "analysis_complete" not in result["observation"]
