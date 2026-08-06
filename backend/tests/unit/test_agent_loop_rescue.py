"""Unit tests for the agent loop-level rescue plumbing (app.ai.agent_v2).

Covers the org-configurable retry budget default and the test-only fault
injector that the sandbox feedback loop uses to exercise the rescue path.
The full retry -> fallback flow inside main_execution is verified end-to-end
by the sandbox e2e (fault injection + real LLM); these tests pin the pure
pieces it depends on.
"""
import pytest

import app.ai.agent_v2 as agent_v2
from app.schemas.organization_settings_schema import OrganizationSettingsConfig


# ── org setting ────────────────────────────────────────────────────────────

def test_agent_loop_retries_defaults_to_2():
    cfg = OrganizationSettingsConfig()
    assert cfg.agent_loop_retries.value == 2
    assert cfg.agent_loop_retries.editable is True


# ── fault injector ─────────────────────────────────────────────────────────

def test_fault_injector_inert_by_default(monkeypatch):
    monkeypatch.setattr(agent_v2, "_LOOP_FAULT_BUDGET", 0)
    for i in range(5):
        agent_v2._maybe_inject_loop_fault(i)  # must not raise


def test_fault_injector_burns_budget_and_stops(monkeypatch):
    monkeypatch.setattr(agent_v2, "_LOOP_FAULT_BUDGET", 2)
    monkeypatch.setattr(agent_v2, "_LOOP_FAULT_MIN_INDEX", 1)

    with pytest.raises(RuntimeError, match="fault-injection"):
        agent_v2._maybe_inject_loop_fault(1)
    with pytest.raises(RuntimeError, match="fault-injection"):
        agent_v2._maybe_inject_loop_fault(1)
    # Budget spent — the loop proceeds normally afterwards.
    agent_v2._maybe_inject_loop_fault(1)
    assert agent_v2._LOOP_FAULT_BUDGET == 0


def test_shrink_factor_uses_provider_numbers():
    """Anthropic overflow messages carry actual vs limit — one retry should
    land under the real limit (ratio × 0.95 margin) instead of walking down."""
    msg = "prompt is too long: 250000 tokens > 200000 maximum"
    assert agent_v2._shrunk_context_factor(1.0, msg) == pytest.approx(0.76)


def test_shrink_factor_parses_openai_format():
    """OpenAI states the limit first, the actual second — order must map."""
    msg = ("This model's maximum context length is 128000 tokens. "
           "However, your messages resulted in 130000 tokens.")
    assert agent_v2._shrunk_context_factor(1.0, msg) == pytest.approx((128000 / 130000) * 0.95)


def test_shrink_factor_parses_gemini_format():
    msg = "The input token count (1200000) exceeds the maximum number of tokens allowed (1048576)."
    assert agent_v2._shrunk_context_factor(1.0, msg) == pytest.approx((1048576 / 1200000) * 0.95)


def test_shrink_factor_decays_without_numbers():
    assert agent_v2._shrunk_context_factor(1.0, "opaque provider error") == pytest.approx(0.85)


def test_shrink_factor_always_makes_progress():
    """A parsed ratio that wouldn't shrink below the current factor decays
    instead — a second overflow at the same factor must still cut further."""
    msg = "prompt is too long: 250000 tokens > 200000 maximum"
    again = agent_v2._shrunk_context_factor(0.76, msg)
    assert again < 0.76


def test_shrink_factor_floors_at_20_percent():
    assert agent_v2._shrunk_context_factor(0.21, "opaque") == pytest.approx(0.2)
    assert agent_v2._shrunk_context_factor(0.2, None) == pytest.approx(0.2)


def test_context_fault_kind_classifies_as_context_length(monkeypatch):
    """The 'context' fault kind must produce exactly what a real Anthropic
    overflow produces, or the e2e exercise proves nothing."""
    from app.ai.llm.errors import classify
    monkeypatch.setattr(agent_v2, "_LOOP_FAULT_BUDGET", 1)
    monkeypatch.setattr(agent_v2, "_LOOP_FAULT_MIN_INDEX", 1)
    monkeypatch.setattr(agent_v2, "_LOOP_FAULT_KIND", "context")

    with pytest.raises(RuntimeError) as exc_info:
        agent_v2._maybe_inject_loop_fault(1)
    classified = classify(exc_info.value, provider="anthropic", model="claude-haiku-4-5-20251001")
    assert classified.code == "context_length"


def test_fault_injector_waits_for_min_index(monkeypatch):
    """Faults fire mid-run (after at least one real step), never at index 0 —
    that's what makes the sandbox scenario 'an error mid multi-step run'."""
    monkeypatch.setattr(agent_v2, "_LOOP_FAULT_BUDGET", 1)
    monkeypatch.setattr(agent_v2, "_LOOP_FAULT_MIN_INDEX", 1)

    agent_v2._maybe_inject_loop_fault(0)  # below min index: no fault
    assert agent_v2._LOOP_FAULT_BUDGET == 1
    with pytest.raises(RuntimeError):
        agent_v2._maybe_inject_loop_fault(1)


# ── planner_v3 pre-classification ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_planner_stream_error_carries_preclassified_payload():
    """planner_v3 must classify the TYPED exception at catch time and stash
    the payload on PlannerError.details — str(exc) on a botocore ClientError
    loses the HTTP status, and the agent's string re-classification then
    misses context_length entirely (Bedrock's overflow path)."""
    import types
    from app.ai.agents.planner.planner_v3 import PlannerV3
    from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
    from app.schemas.ai.planner import PlannerInput

    planner = PlannerV3.__new__(PlannerV3)  # skip __init__ (constructs a real LLM)
    planner.tool_catalog = []
    planner.prompt_builder = PromptBuilderV3()
    planner._tool_category = {}

    class _ClientError(Exception):
        response = {"ResponseMetadata": {"HTTPStatusCode": 400}}

    async def _raising_stream(**kwargs):
        raise _ClientError(
            "An error occurred (ValidationException) when calling Converse: "
            "Input is too long for requested model."
        )
        yield  # pragma: no cover — makes this an async generator

    planner.llm = types.SimpleNamespace(
        model=types.SimpleNamespace(
            model_id="claude-haiku-4-5-20251001",
            provider=types.SimpleNamespace(provider_type="bedrock"),
        ),
        inference_stream_v2=_raising_stream,
    )

    events = []
    async for evt in planner.execute(PlannerInput(user_message="hi"), sigkill_event=None):
        events.append(evt)

    final = [e for e in events if getattr(e, "type", "") == "planner.decision.final"]
    assert final, "expected a final decision event"
    err = final[-1].data.error
    assert err is not None and err.code == "stream_error"
    pre = (err.details or {}).get("llm_error")
    assert isinstance(pre, dict)
    assert pre.get("code") == "context_length"


# ── planner_v3 empty-stream rejection ──────────────────────────────────────
#
# A stream that ends with nothing but a stop + usage (a provider refusal, a
# stop reason or content-block type the adapter doesn't recognize) used to
# fold into a schema-valid, error-free, completely EMPTY decision. The agent
# loop then had nothing to retry on and replayed the identical prompt until
# the step limit — observed in production as 100 blank plan decisions and
# ~450k prompt tokens finalized as "success". These tests pin the planner
# boundary: no semantic output ⇒ typed error, with the raw stop reason and
# stream event mix preserved; real tool-only and text-only streams stay valid.

def _bare_planner():
    import types
    from app.ai.agents.planner.planner_v3 import PlannerV3
    from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3

    planner = PlannerV3.__new__(PlannerV3)  # skip __init__ (constructs a real LLM)
    planner.tool_catalog = []
    planner.prompt_builder = PromptBuilderV3()
    planner._tool_category = {}
    planner.llm = types.SimpleNamespace(
        model=types.SimpleNamespace(
            model_id="any-model",
            provider=types.SimpleNamespace(provider_type="custom"),
        ),
    )
    return planner


async def _final_decision(planner):
    import asyncio
    from app.schemas.ai.planner import PlannerInput

    final = None
    async for evt in planner.execute(PlannerInput(user_message="hi"), sigkill_event=asyncio.Event()):
        if getattr(evt, "type", "") == "planner.decision.final":
            final = evt.data
    assert final is not None, "expected a final decision event"
    return final


@pytest.mark.asyncio
async def test_planner_rejects_stream_without_semantic_output():
    """Stop + usage only — the exact production runaway shape (a few
    completion tokens, zero text/tool events, unmapped stop reason)."""
    from app.ai.llm.types import MessageStopEvent, UsageEvent

    planner = _bare_planner()

    async def _empty_stream(**kwargs):
        yield MessageStopEvent(stop_reason="other", raw_stop_reason="content_filter")
        yield UsageEvent(input_tokens=4478, output_tokens=4, cache_creation_tokens=8000)

    planner.llm.inference_stream_v2 = _empty_stream

    final = await _final_decision(planner)
    assert final.error is not None, "an empty stream must not produce a valid decision"
    assert final.error.code == "empty_response"
    assert final.analysis_complete is False  # an empty turn is never terminal
    assert not final.actions and final.action is None
    # Diagnostics survive: raw stop reason, event mix, token usage.
    details = final.error.details or {}
    assert details.get("raw_stop_reason") == "content_filter"
    assert details.get("stream_event_counts", {}).get("message_stop") == 1
    assert final.metrics is not None and final.metrics.token_usage is not None
    assert final.metrics.token_usage.prompt_tokens == 4478


@pytest.mark.asyncio
async def test_planner_rejects_empty_end_turn():
    """end_turn with no content is equally useless — it used to finalize the
    turn as a successful answer with empty text."""
    from app.ai.llm.types import MessageStopEvent, UsageEvent

    planner = _bare_planner()

    async def _empty_stream(**kwargs):
        yield MessageStopEvent(stop_reason="end_turn", raw_stop_reason="end_turn")
        yield UsageEvent(input_tokens=100, output_tokens=1)

    planner.llm.inference_stream_v2 = _empty_stream

    final = await _final_decision(planner)
    assert final.error is not None and final.error.code == "empty_response"
    assert final.analysis_complete is False


@pytest.mark.asyncio
async def test_planner_accepts_tool_only_stream():
    """A native tool call without narration is the normal v3 shape — the
    rejection must not touch it."""
    from app.ai.llm.types import (
        MessageStopEvent, ToolUseCompleteEvent, ToolUseStartEvent, UsageEvent,
    )

    planner = _bare_planner()

    async def _tool_stream(**kwargs):
        yield ToolUseStartEvent(id="tu_1", name="create_data")
        yield ToolUseCompleteEvent(id="tu_1", name="create_data", input={"title": "x"})
        yield MessageStopEvent(stop_reason="tool_use", raw_stop_reason="tool_calls")
        yield UsageEvent(input_tokens=100, output_tokens=20)

    planner.llm.inference_stream_v2 = _tool_stream

    final = await _final_decision(planner)
    assert final.error is None
    assert final.actions and final.actions[0].name == "create_data"


@pytest.mark.asyncio
async def test_planner_accepts_text_only_stream():
    """A plain final answer (text + end_turn) stays a terminal decision."""
    from app.ai.llm.types import MessageStopEvent, TextDeltaEvent, UsageEvent

    planner = _bare_planner()

    async def _text_stream(**kwargs):
        yield TextDeltaEvent(text="All done: revenue grew 8%.")
        yield MessageStopEvent(stop_reason="end_turn", raw_stop_reason="stop")
        yield UsageEvent(input_tokens=100, output_tokens=10)

    planner.llm.inference_stream_v2 = _text_stream

    final = await _final_decision(planner)
    assert final.error is None
    assert final.analysis_complete is True
    assert (final.assistant_message or "").startswith("All done")
