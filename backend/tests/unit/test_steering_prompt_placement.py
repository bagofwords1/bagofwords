"""Steering must land where the planner actually drives from.

After the first iteration the prompt demotes <original_user_prompt> to
background context ("the real driver is the observation"), so steering buried
there gets ignored on plan-driven runs. The contract: when
PlannerInput.steering_context is set, the rendered prompt carries it AFTER
<last_observation> — the end-of-prompt position the planner acts on.
"""
from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
from app.schemas.ai.planner import PlannerInput


def _render(**kwargs) -> str:
    return PromptBuilderV3._build_user_message(PlannerInput(user_message="original ask", **kwargs))


def test_steering_context_renders_after_last_observation():
    out = _render(
        steering_context="<steering_updates>pivot to albums</steering_updates>",
        last_observation={"summary": "created sales table"},
    )
    steer_idx = out.find("<steering_updates>")
    obs_idx = out.find("<last_observation>")
    assert obs_idx > -1
    assert steer_idx > obs_idx, "steering must render after last_observation, not buried earlier"
    assert "pivot to albums" in out


def test_no_steering_block_when_absent():
    out = _render(last_observation={"summary": "x"})
    assert "<steering_updates>" not in out


def test_steering_present_on_first_iteration_too():
    # No observation yet (first loop) — steering still renders.
    out = _render(steering_context="<steering_updates>now</steering_updates>")
    assert "<steering_updates>" in out
