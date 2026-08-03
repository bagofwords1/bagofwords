"""The compaction summary must survive the prompt layout.

`messages_context` carries the rolling `<history_summary>` produced by
`context_compaction_service`. Phase 3 moved that block out of the static
prefix and into the per-turn head so the cached prefix stays byte-stable
within a run. The failure mode that move introduces is silent: the summary
stops reaching the model, the agent forgets everything behind the compaction
watermark, and nothing errors.

These pin it on both prompt paths — transcript and legacy — so a future
layout change has to break a test rather than a conversation.
"""
import pytest

from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
from app.schemas.ai.planner import PlannerInput


SUMMARY = (
    "<history_summary>\n"
    "Summary of earlier conversation turns (compacted history):\n"
    'Opening request (the first ask in this report): "What files do you have access to?"\n'
    "Critical context: Total revenue: $265,370.23\n"
    "</history_summary>"
)


def _planner_input(**kw) -> PlannerInput:
    return PlannerInput(
        user_message="What was the total revenue you calculated earlier?",
        messages_context=SUMMARY,
        **kw,
    )


def _flatten(messages) -> str:
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    out.append(part["text"])
    return "\n".join(out)


def test_turn_head_carries_the_summary():
    head = PromptBuilderV3._build_turn_head(_planner_input())
    assert "<history_summary>" in head
    assert "265,370.23" in head


def test_static_context_does_not_carry_the_summary():
    """It rides with the volatile head, not the cached prefix — if it leaks
    back into the static block the prefix churns every iteration."""
    static = PromptBuilderV3._build_static_context(_planner_input())
    assert "<history_summary>" not in static


@pytest.mark.parametrize("use_transcript", [True, False])
def test_summary_reaches_the_model_on_both_paths(monkeypatch, use_transcript):
    monkeypatch.setenv("BOW_PLANNER_TRANSCRIPT", "1" if use_transcript else "0")
    built = PromptBuilderV3.build(_planner_input(use_transcript=use_transcript))
    body = _flatten(built.messages)
    assert "<history_summary>" in body, "compaction summary never reached the payload"
    assert "265,370.23" in body


def test_summary_survives_a_tight_transcript_budget(monkeypatch):
    """Decay trims tool results, never the head the summary rides in. A budget
    small enough to force the ladder must still leave the summary standing."""
    monkeypatch.setenv("BOW_PLANNER_TRANSCRIPT", "1")
    monkeypatch.setenv("BOW_TRANSCRIPT_BUDGET_TOKENS", "1")
    built = PromptBuilderV3.build(_planner_input(use_transcript=True))
    assert "<history_summary>" in _flatten(built.messages)
