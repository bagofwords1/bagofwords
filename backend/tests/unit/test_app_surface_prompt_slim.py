"""An app conversation's prompt carries no artifact-building guidance.

App conversations (``bow.conversations`` / ``useConversation``: the
``artifact_chat`` platform with ``platform_context["surface"] == "app"``) are
answered by an agent that only has data tools: it answers a person's data
question and never builds or edits an app. The artifact authoring reference
(SDK, design and runtime docs, verification and screenshot policy, slides and
edit-op contracts), the DASHBOARDS and DOCUMENTS routing, the create_artifact
example and the builder-only ``<app_activity>`` block are left out there. The
data guidance, the app's own clauses (identity, RESULTS SURFACE, ARITHMETIC),
the language rule, instructions and the data-visibility gate stay.

Every other turn (the dashboard bubble, a normal report or builder turn) keeps
the full builder prompt.
"""
from __future__ import annotations

import pytest

from app.ai.agents.planner.artifact_authoring import build_artifact_authoring_reference
from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
from app.schemas.ai.planner import PlannerInput

APP = {"artifact_title": "Sales overview", "source_report_title": "Sales overview", "scope": "agents",
       "surface": "app"}
BUBBLE = {"artifact_title": "Sales overview", "source_report_title": "Sales overview", "scope": "agents"}
ACTIVITY = "<app_activity>! v2 bow.query failed (500)</app_activity>"

# Stable markers of the builder guidance, each from a different section.
BUILDER_MARKERS = (
    "ARTIFACT AUTHORING REFERENCE",                  # the authoring reference header
    "ARTIFACT RESOURCE SDK v1",                      # artifact_sdk_reference.py
    "useArtifactData",                               # runtime / SDK docs
    "verification_hint.automatic_check",             # ARTIFACT_VERIFICATION_POLICY
    "READING THE VALIDATION SCREENSHOT",             # screenshot contract
    "edit_artifact",
    "create_artifact",
    "browser_navigate",
    "<app_activity>",
)


def _input(ctx=None, platform="artifact_chat", **kw) -> PlannerInput:
    return PlannerInput(
        user_message="Show total revenue per month", organization_name="Acme",
        organization_ai_analyst_name="Analyst", external_platform=platform, platform_context=ctx,
        instructions="<instructions>Revenue is net of refunds.</instructions>",
        app_activity_context=ACTIVITY, **kw)


@pytest.fixture(params=["1", "0"], ids=["transcript", "legacy"])
def v3_path(request, monkeypatch):
    monkeypatch.setenv("BOW_PLANNER_TRANSCRIPT", request.param)
    return request.param


@pytest.mark.parametrize("marker", BUILDER_MARKERS)
def test_an_app_conversation_has_no_builder_guidance(marker, v3_path):
    prompt = PromptBuilderV3.build_prompt(_input(APP))
    assert marker not in prompt


def test_an_app_conversation_keeps_what_an_answering_agent_needs(v3_path):
    prompt = PromptBuilderV3.build_prompt(_input(APP, allow_llm_see_data=False))
    for kept in (
        "You are the assistant inside an app built on BOW",    # app identity
        "RESULTS SURFACE",
        "ARITHMETIC",
        '  App: "Sales overview"',
        "ANALYTICS STANDARDS",                                  # data-tool guidance
        "ROUTING (classify the ask first",
        "create_data",
        "LANGUAGE",                                             # global language rule
        "Revenue is net of refunds.",                           # instructions
        "<data_visibility>Data privacy mode is ON",             # data-visibility gate (legacy path)
    ):
        if kept.startswith("<data_visibility>") and v3_path == "1":
            continue  # the transcript path carries no data_visibility block at HEAD either
        assert kept in prompt, kept


@pytest.mark.parametrize("ctx,platform", [(BUBBLE, "artifact_chat"), (None, None), ({**APP, "surface": "x"},
                                                                                     "artifact_chat")],
                         ids=["bubble", "report", "unknown-surface"])
def test_every_other_turn_keeps_the_builder_prompt(ctx, platform, v3_path):
    pi = _input(ctx, platform)
    v3 = PromptBuilderV3.build(pi)
    assert v3.system.endswith("\n\n" + build_artifact_authoring_reference())
    assert "\nDASHBOARDS\n" in v3.system and "\nDOCUMENTS\n" in v3.system
    assert ACTIVITY in PromptBuilderV3.build_prompt(pi)


def test_the_app_conversation_prompt_is_much_smaller(v3_path):
    app = len(PromptBuilderV3.build_prompt(_input(APP)))
    bubble = len(PromptBuilderV3.build_prompt(_input(BUBBLE)))
    assert bubble - app > 30000


def test_an_app_conversation_is_offered_no_artifact_tool():
    # read_artifact was still offered there: its description (about 6K chars)
    # teaches reading code before edit_artifact, and an app conversation's own
    # report holds no artifact to read.
    from app.ai.agent_v2 import ARTIFACT_CHAT_TOOL_ALLOWLIST, artifact_chat_tool_allowlist

    app = artifact_chat_tool_allowlist(APP)
    assert not {t for t in app if "artifact" in t or t.startswith("browser_")}
    assert {"create_data", "run_query", "read_query", "describe_tables", "inspect_data"} <= app
    agent_tools = {"execute_mcp", "search_tools", "call_tool",
                   "list_mcp_resources", "read_mcp_resource", "submit_list"}
    assert agent_tools <= app
    assert not agent_tools & ARTIFACT_CHAT_TOOL_ALLOWLIST
    assert not {"create_artifact", "edit_artifact", "create_instruction", "send_email"} & app
    # The dashboard bubble keeps its tools, read_artifact included.
    for ctx in (BUBBLE, None, {**APP, "surface": "bubble"}):
        assert artifact_chat_tool_allowlist(ctx) == ARTIFACT_CHAT_TOOL_ALLOWLIST
    assert "read_artifact" in ARTIFACT_CHAT_TOOL_ALLOWLIST
