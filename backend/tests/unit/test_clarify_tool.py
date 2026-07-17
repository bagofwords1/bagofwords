"""Unit tests for the clarify tool schema + streaming contract.

The interactive form itself lives in the frontend (ClarifyTool.vue); the live
loop is exercised in docs/feedback-loops/clarify-multi-pick.md. Here we cover
the deterministic pieces:
  - input validation (questions required, non-empty)
  - multi_select is optional, defaults to single-pick, and is exposed in the
    JSON schema the LLM sees
  - run_stream forwards each question (including multi_select) to the UI via
    the tool.start payload and ends the turn (analysis_complete)
"""
from __future__ import annotations

import pytest

from app.ai.tools.implementations.clarify import ClarifyTool
from app.ai.tools.schemas import ClarifyInput


async def _collect(tool, tool_input, ctx):
    events = []
    async for evt in tool.run_stream(tool_input, ctx):
        events.append(evt)
    return events


# --- input validation --------------------------------------------------------


def test_questions_required_and_non_empty():
    with pytest.raises(Exception):
        ClarifyInput()
    with pytest.raises(Exception):
        ClarifyInput(questions=[])
    with pytest.raises(Exception):
        ClarifyInput(questions=[{"text": ""}])


def test_multi_select_defaults_to_single_pick():
    data = ClarifyInput(questions=[{"text": "Which range?", "options": ["A", "B"]}])
    assert data.questions[0].multi_select is False


def test_multi_select_accepted():
    data = ClarifyInput(questions=[
        {"text": "Which metrics?", "options": ["Revenue", "Orders"], "multi_select": True},
        {"text": "Chart title?"},
    ])
    assert data.questions[0].multi_select is True
    assert data.questions[1].multi_select is False


def test_multi_select_present_in_llm_schema():
    """The LLM can only use the field if it appears in the advertised schema."""
    schema = ClarifyTool().metadata.input_schema
    question_props = schema["$defs"]["ClarifyQuestion"]["properties"]
    assert "multi_select" in question_props


# --- streaming contract ------------------------------------------------------


@pytest.mark.asyncio
async def test_start_payload_carries_multi_select_to_ui():
    tool = ClarifyTool()
    events = await _collect(
        tool,
        {
            "questions": [
                {"text": "Which metrics?", "options": ["Revenue", "Orders"], "multi_select": True},
                {"text": "Which range?", "options": ["7d", "30d"]},
                {"text": "Anything else?"},
            ],
        },
        {},
    )

    start = [e for e in events if e.type == "tool.start"]
    assert start, "expected a tool.start event"
    qs = start[0].payload["questions"]
    assert [q.get("multi_select") for q in qs] == [True, False, False]
    # The UI renders straight from these dicts — options must survive too.
    assert qs[0]["options"] == ["Revenue", "Orders"]

    end = [e for e in events if e.type == "tool.end"]
    assert end, "expected a tool.end event"
    obs = end[-1].payload["observation"]
    # Clarify always ends the turn and waits for the user.
    assert obs["analysis_complete"] is True
    assert end[-1].payload["output"]["status"] == "awaiting_response"
