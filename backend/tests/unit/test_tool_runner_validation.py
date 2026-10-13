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


class _ChatOnlySpec:
    allowed_modes = ["chat", "training"]


class _ChatOnlyTool(_FakeTool):
    name = "create_data"
    spec = _ChatOnlySpec()


class _SubagentOnlySpec:
    allowed_modes = ["subagent"]


class _SubagentOnlyTool(_FakeTool):
    name = "finish_subtask"
    spec = _SubagentOnlySpec()


@pytest.mark.asyncio
async def test_mode_gate_treats_a_sub_run_as_chat():
    """A sub-agent (docs/design/sub-runs.md) is a chat-mode run with a narrower
    catalog enforced before dispatch, so a chat tool its profile allows must
    not be refused here; a plain 'subagent' mode without a sub_run_id, and a
    subagent-only tool in chat, still are."""
    runner = ToolRunner()
    ok = await runner.run(_ChatOnlyTool(), {"user_prompt": "x"}, {"mode": "subagent", "sub_run_id": "sr-1"}, _noop_emit)
    assert ok["observation"]["success"] is True

    refused = await runner.run(_ChatOnlyTool(), {"user_prompt": "x"}, {"mode": "subagent"}, _noop_emit)
    assert refused["error"]["type"] == "mode_not_allowed"

    refused_in_chat = await runner.run(_SubagentOnlyTool(), {"user_prompt": "x"}, {"mode": "chat"}, _noop_emit)
    assert refused_in_chat["error"]["type"] == "mode_not_allowed"
