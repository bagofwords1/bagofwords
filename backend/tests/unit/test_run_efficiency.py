"""In-run efficiency readout contracts (planner/run_efficiency.py).

- Silent on short runs with nothing repeated; appears once the run is long or
  as soon as an identical call repeats.
- Counts identical calls only (same tool, same arguments, argument order
  irrelevant); polling tools are never flagged.
- Reports input tokens with the share served from cache.
- Rendered in the volatile per-turn head, never in the system prompt, so the
  cached prefix is byte-stable whether or not the line is present.
"""
import json

from app.ai.agents.planner import run_efficiency
from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
from app.ai.agents.planner.run_efficiency import (
    RunEfficiency, SHOW_AFTER_INPUT_TOKENS, SHOW_AFTER_ITERATIONS,
)
from app.schemas.ai.planner import PlannerInput, TokenUsage


def _usage(prompt, completion, cached=0):
    return TokenUsage(prompt_tokens=prompt, completion_tokens=completion,
                      total_tokens=prompt + completion, cache_read_tokens=cached or None)


def test_silent_on_a_short_run_without_repeats():
    eff = RunEfficiency()
    eff.add_planner_usage(_usage(9_000, 200, 6_000))
    eff.add_tool_call("describe_tables", {"tables": ["orders"]})
    eff.add_tool_call("create_data", {"title": "Revenue"})
    for step in range(SHOW_AFTER_ITERATIONS):
        assert eff.render(step) == ""


def test_long_run_reports_tokens_and_cache_share():
    eff = RunEfficiency()
    for _ in range(3):
        eff.add_planner_usage(_usage(10_000, 500, 7_500))
    line = eff.render(SHOW_AFTER_ITERATIONS + 2)
    assert line.startswith("<run_efficiency>") and line.endswith("</run_efficiency>")
    assert "30,000" in line          # summed input
    assert "75% from cache" in line  # 22,500 / 30,000
    assert "1,500" in line           # summed output
    assert "never mention" in line   # not for the user
    assert "repeated" not in line


def test_identical_call_repeat_shows_immediately_regardless_of_argument_order():
    eff = RunEfficiency()
    eff.add_tool_call("inspect_data", {"table": "orders", "limit": 5})
    eff.add_tool_call("inspect_data", {"limit": 5, "table": "orders"})
    line = eff.render(1)
    assert "inspect_data ×2" in line
    assert "reuse that earlier result" in line


def test_same_tool_with_different_arguments_is_not_a_repeat():
    eff = RunEfficiency()
    for table in ("orders", "customers", "refunds"):
        eff.add_tool_call("inspect_data", {"table": table})
    assert eff.render(1) == ""


def test_polling_tools_are_never_flagged():
    eff = RunEfficiency()
    for _ in range(5):
        eff.add_tool_call("wait", {"seconds": 30})
    assert eff.render(1) == ""


def test_missing_usage_and_unserializable_input_do_not_raise():
    eff = RunEfficiency()
    eff.add_planner_usage(None)
    eff.add_tool_call("create_data", {"obj": object()})
    eff.add_tool_call(None, {})
    assert eff.planner_calls == 0
    assert eff.cache_share() is None


def test_flag_is_off_by_default(monkeypatch):
    monkeypatch.delenv("BOW_RUN_EFFICIENCY", raising=False)
    assert run_efficiency.enabled() is False
    monkeypatch.setenv("BOW_RUN_EFFICIENCY", "1")
    assert run_efficiency.enabled() is True
    monkeypatch.setenv("BOW_RUN_EFFICIENCY", "off")
    assert run_efficiency.enabled() is False


def _built(**kw):
    return PromptBuilderV3.build(PlannerInput(user_message="How many orders?", **kw))


def test_line_rides_in_the_newest_user_turn_not_the_system_prompt():
    eff = RunEfficiency()
    eff.add_tool_call("inspect_data", {"table": "orders"})
    eff.add_tool_call("inspect_data", {"table": "orders"})
    line = eff.render(2)

    with_line = _built(run_efficiency=line)
    without = _built()

    assert line not in with_line.system
    assert with_line.system == without.system
    last_user = [m for m in with_line.messages if m["role"] == "user"][-1]
    assert line in json.dumps(last_user["content"], ensure_ascii=False)
    # The first user turn (the cacheable static context) is unchanged.
    first_with = json.dumps(with_line.messages[0]["content"], ensure_ascii=False)
    first_without = json.dumps(without.messages[0]["content"], ensure_ascii=False)
    assert len(with_line.messages) > 1
    assert first_with == first_without


def test_cache_share_means_the_same_on_every_provider():
    """Anthropic's prompt_tokens excludes cached input; OpenAI's includes it.
    The same request (10k input, 8k of it cached) must read the same."""
    anthropic, openai = RunEfficiency(), RunEfficiency()
    anthropic.add_planner_usage(
        TokenUsage(prompt_tokens=1_500, completion_tokens=100, cache_read_tokens=8_000,
                   cache_creation_tokens=500), provider="anthropic")
    openai.add_planner_usage(
        TokenUsage(prompt_tokens=10_000, completion_tokens=100, cache_read_tokens=8_000),
        provider="openai")
    assert anthropic.input_tokens == openai.input_tokens == 10_000
    assert anthropic.cache_share() == openai.cache_share() == 0.8


def test_large_run_shows_before_the_step_threshold():
    """Parallel tool batches keep the step count low; input size still counts."""
    eff = RunEfficiency()
    eff.add_planner_usage(_usage(SHOW_AFTER_INPUT_TOKENS, 300, SHOW_AFTER_INPUT_TOKENS // 2))
    assert eff.render(1).startswith("<run_efficiency>")
    small = RunEfficiency()
    small.add_planner_usage(_usage(SHOW_AFTER_INPUT_TOKENS // 4, 300))
    assert small.render(1) == ""
