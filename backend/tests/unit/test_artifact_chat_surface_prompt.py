"""Which surface an ``artifact_chat`` message is answered on.

The dashboard's chat bubble shows only the agent's text, so its prompt stays
TEXT-ONLY. An app's conversation (``bow.conversations``) shows each
create_data result as a table (and a chart when it fits), so the agent is told
what the viewer does and does not see. The two share threads, so the surface
is per message: ``platform_context["surface"] == "conversation"``.

The bubble blocks below are a snapshot captured at b6363c4f1 and hand-reviewed
against prompt_builder.py (``_format_artifact_chat_context``, the two viewer
lines, the title line, the data-only scope with a visualization, a withheld
visualization, and the agents scope), line by line: they are the intended
bubble text, not merely the current output.
"""
from __future__ import annotations

import re

import pytest

from app.ai.agents.planner.prompt_builder import PromptBuilder
from app.schemas.ai.planner import PlannerInput

VIEWER = ('  You are answering questions from a VIEWER of a shared dashboard, not its owner. Keep answers concise '
          'and grounded in the dashboard.')
TEXT_ONLY = ('  TEXT-ONLY SURFACE: the viewer chats in a small text bubble and sees ONLY your text replies — tool '
             'outputs, tables, and widgets you produce are NOT visible to them. Every answer must be self-contained: '
             'present the relevant data inline as short markdown lists or tables. NEVER refer to a table or result as '
             '"above", "available", or "shown" — nothing but your text is shown. For large results, include the most '
             'relevant rows (up to ~20) inline and say how many rows the full result has.')
TITLE = '  Dashboard: "Sales overview"'
DATA_ONLY = ("  Scope: the dashboard's own data ONLY (below). You have no query tools and no other data access — if a "
             "question needs data beyond this, say so plainly.")

CONTEXTS = {
    "agents": {"artifact_title": "Sales overview", "source_report_title": "Sales overview", "scope": "agents"},
    "data_only": {"artifact_title": "Sales overview", "scope": "data_only", "visualizations": [
        {"title": "Revenue by genre", "columns": ["Genre", "Revenue"], "rows": [{"Genre": "Rock", "Revenue": 80.19}],
         "total_rows": 3}]},
    "withheld": {"artifact_title": "Sales overview", "scope": "data_only",
                 "visualizations": [{"title": "My orders", "withheld": True}]},
}

BUBBLE = {
    "agents": "\n".join([
        "<artifact_chat_context>", VIEWER, TEXT_ONLY, TITLE,
        "  Scope: you may query the attached agents (read-only) to answer questions about this dashboard.",
        "</artifact_chat_context>"]),
    "data_only": "\n".join([
        "<artifact_chat_context>", VIEWER, TEXT_ONLY, TITLE, DATA_ONLY,
        "  <visualization>", '    title: "Revenue by genre"', '    columns: ["Genre", "Revenue"]',
        "    rows (showing 1 of 3):", '    [{"Genre": "Rock", "Revenue": 80.19}]', "  </visualization>",
        "</artifact_chat_context>"]),
    "withheld": "\n".join([
        "<artifact_chat_context>", VIEWER, TEXT_ONLY, TITLE, DATA_ONLY,
        "  <visualization>", '    title: "My orders"',
        "    rows: NOT AVAILABLE TO THIS VIEWER — this chart runs under each viewer's own identity and they have no "
        "result of their own yet. Say the data for this chart is not available to them and suggest opening the "
        "dashboard; never estimate or infer its values.",
        "  </visualization>",
        "</artifact_chat_context>"]),
}


def _block(ctx) -> str:
    prompt = PromptBuilder.build_prompt(PlannerInput(
        user_message="Show total revenue per month", organization_name="Acme", organization_ai_analyst_name="Analyst",
        external_platform="artifact_chat", platform_context=ctx))
    found = re.findall(r"<artifact_chat_context>.*?</artifact_chat_context>", prompt, re.S)
    assert len(found) == 1, "one artifact chat block per prompt"
    return found[0]


@pytest.mark.parametrize("name", sorted(CONTEXTS))
def test_the_bubble_prompt_is_unchanged(name):
    assert _block(CONTEXTS[name]) == BUBBLE[name]


@pytest.mark.parametrize("name", sorted(CONTEXTS))
def test_a_conversation_message_is_told_which_results_the_viewer_sees(name):
    block = _block({**CONTEXTS[name], "surface": "conversation"})
    assert "TEXT-ONLY SURFACE" not in block and "VIEWER of a shared dashboard" not in block
    assert "RESULTS SURFACE" in block
    assert "create_data result" in block
    for hidden in ("run_query", "read_query", "inspect_data"):
        assert hidden in block  # named as not shown
    assert "report" in block and "the chart" in block  # told never to point at either
    # The rest of the block is the same as the bubble's.
    rest = [x for x in BUBBLE[name].split("\n") if x not in (VIEWER, TEXT_ONLY)]
    assert [x for x in block.split("\n") if x in rest] == rest


def test_an_unknown_surface_is_the_bubble():
    assert _block({**CONTEXTS["agents"], "surface": "bubble"}) == BUBBLE["agents"]
