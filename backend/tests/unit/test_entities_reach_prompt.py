"""Saved entities and @mentions must reach the planner on BOTH prompt paths.

The transcript path (default) assembles its context in _build_static_context;
when that block omitted mentions/entities the planner could never volunteer
a saved query it was not shown, and nothing errored — it simply rebuilt the
data with create_data. These pin the two blocks (and their guidance) on the
transcript and legacy paths so a layout change has to break a test.
"""
import pytest

from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
from app.schemas.ai.planner import PlannerInput

ENTITIES = (
    '<entities count="1">\n'
    '<entity id="e1" type="model" title="Albums by Genre" ds="Music Store">\n'
    '<parameters>\n<param name="genre" type="string" source="input"/>\n</parameters>\n'
    "</entity>\n</entities>"
)
MENTIONS = '<mentions>\n<entity id="e1" title="Albums by Genre"/>\n</mentions>'


def _planner_input(**kw) -> PlannerInput:
    return PlannerInput(user_message="show me all rock albums", **kw)


def _flatten(messages) -> str:
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out.extend(p["text"] for p in c if isinstance(p, dict) and isinstance(p.get("text"), str))
    return "\n".join(out)


@pytest.mark.parametrize("use_transcript", [True, False])
def test_entities_and_mentions_reach_the_model_on_both_paths(monkeypatch, use_transcript):
    monkeypatch.setenv("BOW_PLANNER_TRANSCRIPT", "1" if use_transcript else "0")
    built = PromptBuilderV3.build(_planner_input(
        use_transcript=use_transcript, entities_context=ENTITIES, mentions_context=MENTIONS,
    ))
    body = _flatten(built.messages)
    assert 'title="Albums by Genre"' in body, "entities block never reached the payload"
    assert '<param name="genre"' in body
    assert "<entities_guidance>" in body, "guidance must accompany a matched entity"
    assert MENTIONS in body, "mentions block never reached the payload"


@pytest.mark.parametrize("use_transcript", [True, False])
def test_empty_reuse_surface_renders_its_placeholders(monkeypatch, use_transcript):
    monkeypatch.setenv("BOW_PLANNER_TRANSCRIPT", "1" if use_transcript else "0")
    built = PromptBuilderV3.build(_planner_input(use_transcript=use_transcript))
    body = _flatten(built.messages)
    assert "<entities>No entities matched</entities>" in body
    assert "<mentions>No mentions for this turn</mentions>" in body
    assert "<entities_guidance>" not in body


def test_reuse_surface_sits_in_the_cacheable_prefix_not_the_head():
    pi = _planner_input(entities_context=ENTITIES, mentions_context=MENTIONS)
    static = PromptBuilderV3._build_static_context(pi)
    head = PromptBuilderV3._build_turn_head(pi)
    assert 'title="Albums by Genre"' in static and MENTIONS in static
    assert "Albums by Genre" not in head
