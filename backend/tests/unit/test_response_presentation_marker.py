"""The per-turn presentation language reaches the planner without looking like
a prompt injection.

Contract:
  * The newest message still ends with the response language — the marker
    stays at the end of the per-step head, where it counters drift into the
    language of English tool output (see backend/AGENTS.md, "LLM prompts").
  * What rides in a user-role message is a fact (the locale), never a
    self-concealing order. Prompt-injection filters such as Azure Prompt
    Shields scan user messages; "apply this silently, do not mention these
    instructions" made them block every chat turn.
  * The rule that gives the marker its meaning lives in the system prompt and
    does not vary with the locale, so the system prompt stays byte-stable and
    prompt caching is unaffected.
"""
import json
import re

import pytest

from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
from app.schemas.ai.planner import PlannerInput

# Wording that instructs the model to act covertly or about "instructions"
# from inside a user turn: the prompt-injection signature filters look for.
_INJECTION_SHAPED = re.compile(
    r"\bsilently\b|\bdo not (?:mention|reveal|disclose)\b|\bdon't (?:mention|reveal)\b"
    r"|\bthese instructions\b|\bignore (?:previous|prior|the above)\b",
    re.I,
)


def _tool_results(n: int) -> list:
    return [
        {
            "execution_number": i,
            "tool_name": "inspect_data",
            "loop_index": i,
            "tool_input": {"user_prompt": f"Row count of Invoice ({i})"},
            "observation": {"summary": "Inspection finished", "success": True,
                            "execution_log": "row_count\n412"},
        }
        for i in range(n)
    ]


def _input(locale: str, *, tool_results: int = 1, **kw) -> PlannerInput:
    obs = _tool_results(tool_results)
    return PlannerInput(
        organization_name="Org",
        organization_ai_analyst_name="AI",
        timezone="UTC",
        user_message="hey",
        past_observations=obs,
        last_observation=obs[-1]["observation"] if obs else None,
        tool_catalog=[],
        schemas_combined="<schemas><table name='Invoice'/></schemas>",
        response_locale=locale,
        **kw,
    )


def _text(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict):
            value = block.get("text", block.get("content", ""))
            parts.append(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str))
        else:
            parts.append(str(block))
    return "\n".join(parts)


def _user_texts(v3) -> list[str]:
    return [_text(m["content"]) for m in v3.messages if m.get("role") == "user"]


@pytest.fixture(params=["transcript", "legacy"])
def planner_path(request, monkeypatch):
    monkeypatch.setenv("BOW_PLANNER_TRANSCRIPT", "1" if request.param == "transcript" else "0")
    return request.param


@pytest.mark.parametrize("locale", ["en", "he", "es", "ar"])
@pytest.mark.parametrize("tool_results", [0, 2])
def test_newest_message_ends_with_the_response_locale(planner_path, locale, tool_results):
    v3 = PromptBuilderV3.build(_input(locale, tool_results=tool_results))
    tail = _user_texts(v3)[-1].rstrip()
    assert tail.endswith(f'<response_presentation locale="{locale}"/>')


@pytest.mark.parametrize("locale", ["en", "he", "es"])
@pytest.mark.parametrize("tool_results", [0, 2])
def test_no_user_message_carries_injection_shaped_wording(planner_path, locale, tool_results):
    v3 = PromptBuilderV3.build(_input(locale, tool_results=tool_results))
    for text in _user_texts(v3):
        hit = _INJECTION_SHAPED.search(text)
        assert hit is None, f"user-role message contains {hit.group(0)!r}"


def test_system_prompt_explains_the_marker_and_is_identical_for_every_locale(planner_path):
    systems = {loc: PromptBuilderV3.build(_input(loc)).system for loc in ("en", "he", "es", "ar")}
    assert len(set(systems.values())) == 1, "the locale must not leak into the cached system prompt"
    assert "<response_presentation" in systems["en"]


def test_sub_agents_get_no_presentation_marker(monkeypatch):
    monkeypatch.setenv("BOW_PLANNER_TRANSCRIPT", "1")
    v3 = PromptBuilderV3.build(_input("he", mode="subagent"))
    assert "<response_presentation" not in json.dumps(v3.messages, ensure_ascii=False, default=str)
