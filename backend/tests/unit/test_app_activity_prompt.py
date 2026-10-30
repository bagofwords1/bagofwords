"""The editor's app failures reach the planner (services/artifact_activity.py):
when a turn carries an <app_activity> section, both prompt layouts render it
inside <context>; a turn without failures renders nothing."""
from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
from app.ai.context.sections.app_activity_section import AppActivitySection
from app.schemas.ai.planner import PlannerInput

FAILURE = "! 12:01:07 editor conversations.create -> error conversation.agent_forbidden  (event 1a2b3c4d)"


def _inputs(**kwargs):
    return PlannerInput(user_message="the app says I can't use the agent", **kwargs)


def test_failures_render_in_both_prompt_layouts():
    section = AppActivitySection(text=FAILURE).render()
    for build in (PromptBuilderV3._build_user_message, PromptBuilderV3._build_static_context):
        out = build(_inputs(app_activity_context=section))
        assert "<app_activity>" in out and "conversation.agent_forbidden" in out
        assert out.index("<app_activity>") < out.index("</context>")


def test_no_section_without_failures():
    assert AppActivitySection(text=None).render() == ""
    for build in (PromptBuilderV3._build_user_message, PromptBuilderV3._build_static_context):
        assert "<app_activity>" not in build(_inputs())
