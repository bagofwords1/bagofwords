"""App conversations use regular tool guidance while keeping app delivery context."""
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
def test_an_app_conversation_has_regular_builder_guidance(marker, v3_path):
    prompt = PromptBuilderV3.build_prompt(_input(APP))
    if marker == "<app_activity>":
        assert ACTIVITY not in prompt  # Host app debugging context belongs to its builder.
    else:
        assert marker in prompt


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


def test_only_dashboard_chat_has_a_special_tool_allowlist():
    from types import SimpleNamespace
    from app.ai.agent_v2 import ARTIFACT_CHAT_TOOL_ALLOWLIST
    from app.services.conversation_surface import is_dashboard_chat

    thread = SimpleNamespace(report_type="artifact_chat")
    assert not is_dashboard_chat(thread, APP)
    assert not is_dashboard_chat(SimpleNamespace(report_type="regular"))
    for ctx in (BUBBLE, None, {**APP, "surface": "bubble"}):
        assert is_dashboard_chat(thread, ctx)
    assert "read_artifact" in ARTIFACT_CHAT_TOOL_ALLOWLIST
    assert not {"execute_mcp", "submit_list", "create_artifact", "send_email"} & ARTIFACT_CHAT_TOOL_ALLOWLIST
