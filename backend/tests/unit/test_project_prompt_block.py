"""The planner user message must carry the <project> block when the report
lives in a project — before org instructions so project framing leads — and
omit it entirely otherwise."""
from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
from app.schemas.ai.planner import PlannerInput


def _base_input(**overrides):
    fields = dict(
        user_message="show revenue",
        instructions="<instructions>org level</instructions>",
        mode="chat",
    )
    fields.update(overrides)
    return PlannerInput(**fields)


def test_project_block_rendered_before_instructions():
    block = (
        "<project>\n  <name>Marketing</name>\n"
        "  <project_instructions>Report revenue in EUR.</project_instructions>\n</project>"
    )
    msg = PromptBuilderV3._build_user_message(_base_input(project_context=block))
    assert "<project>" in msg
    assert "Report revenue in EUR." in msg
    assert msg.index("<project>") < msg.index("org level")


def test_no_project_block_without_project():
    msg = PromptBuilderV3._build_user_message(_base_input())
    assert "<project>" not in msg
