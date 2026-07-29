"""The shared NO OVERFIT block must appear in every prompt that can call
create_instruction / edit_instruction:

- knowledge harness (PlannerV2 / prompt_builder, mode="knowledge")
- training mode on the active builder (PromptBuilderV3, mode="training")
- training mode on the legacy fallback (prompt_builder, BOW_PLANNER=v2)

The block is the model-facing half of the anti-overfit contract; the server
half is the generality gate (rejected_reason='overfit'). If a refactor drops
the block from one of these prompts, overfit attempts rise silently — this
test makes that visible.
"""
import pytest

from app.ai.agents.planner.prompt_blocks import NO_OVERFIT_BLOCK
from app.ai.agents.planner.prompt_builder import PromptBuilder
from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
from app.schemas.ai.planner import PlannerInput

# A stable sentinel from the block — asserting on the full block would make
# the test fail on any wording tweak; the header is the contract.
SENTINEL = "NO OVERFIT — HARD RULE FOR INSTRUCTIONS"


def _input(mode):
    return PlannerInput(user_message="document the orders domain", mode=mode)


def test_block_has_the_essentials():
    assert SENTINEL in NO_OVERFIT_BLOCK
    assert "rejected_reason='overfit'" in NO_OVERFIT_BLOCK
    # The litmus test is the actionable core — keep it.
    assert "DIFFERENT user" in NO_OVERFIT_BLOCK


def test_knowledge_harness_prompt_contains_no_overfit_block():
    prompt = PromptBuilder.build_prompt(_input("knowledge"))
    assert SENTINEL in prompt


def test_v2_training_prompt_contains_no_overfit_block():
    prompt = PromptBuilder.build_prompt(_input("training"))
    assert SENTINEL in prompt


def test_v3_training_system_prompt_contains_no_overfit_block():
    v3 = PromptBuilderV3.build(_input("training"))
    assert SENTINEL in v3.system


def test_v3_chat_prompt_does_not_carry_training_block():
    """Chat mode has no instruction-authoring mandate; the block is scoped to
    modes that write instructions."""
    v3 = PromptBuilderV3.build(_input("chat"))
    assert SENTINEL not in v3.system
