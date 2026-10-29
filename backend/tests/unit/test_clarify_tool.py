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


# --- "Other" is a flag, not an option string ---------------------------------


def test_allow_other_defaults_off_and_is_advertised():
    data = ClarifyInput(questions=[{"text": "Which genre?", "options": ["Rock", "Jazz"]}])
    assert data.questions[0].allow_other is False
    schema = ClarifyTool().metadata.input_schema
    assert "allow_other" in schema["$defs"]["ClarifyQuestion"]["properties"]


def test_examples_never_put_other_in_options():
    """The UI renders its own localized "Other" when allow_other is set; an
    "Other…" option string is what we teach the model NOT to send."""
    for ex in ClarifyTool().metadata.examples:
        for q in ex["input"]["questions"]:
            assert not any(o.strip().lower().startswith("other") for o in q.get("options", []))


@pytest.mark.asyncio
async def test_plain_text_fallback_keeps_each_option_and_the_hints():
    """Channels without the form (Slack, Teams, email) get final_answer. Each
    option must stay readable even when it contains a comma or slash, and the
    multi-select / "Other" affordances must be stated in words."""
    events = await _collect(
        ClarifyTool(),
        {"questions": [
            {"text": "Which metrics?", "options": ["Revenue, net", "Orders / gross"], "multi_select": True, "allow_other": True},
            {"text": "Chart title?"},
        ]},
        {},
    )
    final = [e for e in events if e.type == "tool.end"][-1].payload["observation"]["final_answer"]
    lines = final.splitlines()
    assert "- Revenue, net" in lines and "- Orders / gross" in lines
    assert "select all that apply" in final
    assert "- Other (describe)" in lines
    assert "Chart title?" in lines
