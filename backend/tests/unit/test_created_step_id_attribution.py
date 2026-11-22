"""Which Step a finished tool is credited with (tool_executions.created_step_id).

Chat channels send a tool block's step as its output (chart image, table
link/CSV, count line). Tools that create no step used to inherit the
invocation's last chart, so a dashboard, a generated picture or a failed
dashboard attempt after a chart went out as that chart again — on every
channel — and the generated picture was never delivered.
"""
import pytest

from app.ai.agent_v2 import _INVOCATION_RESET_TOOLS, resolve_created_step_id

CHART_STEP = "chart-step-1"

NON_CREATING_TOOLS = [
    "create_artifact", "edit_artifact", "generate_image", "inspect_data",
    "read_artifact", "execute_mcp", "create_instruction", "clarify",
]


@pytest.mark.parametrize("tool_name", NON_CREATING_TOOLS)
@pytest.mark.parametrize("observation", [None, {}, {"success": True}, {"success": False, "error": "x"}])
def test_a_tool_that_creates_no_step_does_not_inherit_the_last_chart(tool_name, observation):
    assert resolve_created_step_id(tool_name, observation, CHART_STEP) is None


@pytest.mark.parametrize("tool_name", sorted(_INVOCATION_RESET_TOOLS))
def test_a_data_tool_gets_the_step_its_progress_events_created(tool_name):
    assert resolve_created_step_id(tool_name, {"success": True}, CHART_STEP) == CHART_STEP


@pytest.mark.parametrize("tool_name", sorted(_INVOCATION_RESET_TOOLS) + ["read_query", "run_query", "add_parameter"])
def test_a_step_the_tool_reports_itself_wins(tool_name):
    assert resolve_created_step_id(tool_name, {"step_id": "own-step"}, CHART_STEP) == "own-step"


@pytest.mark.parametrize("tool_name", sorted(_INVOCATION_RESET_TOOLS) + NON_CREATING_TOOLS)
def test_nothing_to_credit_without_a_step(tool_name):
    assert resolve_created_step_id(tool_name, {"success": True}, None) is None
