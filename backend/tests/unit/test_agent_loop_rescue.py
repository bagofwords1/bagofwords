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


@pytest.mark.asyncio
async def test_planner_rejects_stream_without_semantic_output():
    """Usage/stop packets alone are not a planner decision.

    Providers can add content-block types faster than our adapters learn them.
    Silently folding an unrecognized stream into ``action=None`` caused the
    agent to replay the same paid request until its 100-step workflow cap.
    The provider boundary must surface a typed, diagnostic error instead.
    """
    import asyncio
    import types

    from app.ai.agents.planner.planner_v3 import PlannerV3
    from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
    from app.ai.llm.types import MessageStopEvent, UsageEvent
    from app.schemas.ai.planner import PlannerInput

    planner = PlannerV3.__new__(PlannerV3)
    planner.tool_catalog = []
    planner.prompt_builder = PromptBuilderV3()
    planner._tool_category = {}

    async def _empty_stream(**kwargs):
        yield MessageStopEvent(stop_reason="other")
        yield UsageEvent(input_tokens=1200, output_tokens=4, cache_creation_tokens=8000)

    planner.llm = types.SimpleNamespace(inference_stream_v2=_empty_stream)

    events = []
    async for evt in planner.execute(
        PlannerInput(user_message="build a dashboard"),
        sigkill_event=asyncio.Event(),
    ):
        events.append(evt)

    final = [e for e in events if getattr(e, "type", "") == "planner.decision.final"][-1].data
    assert final.error is not None
    assert final.error.code == "empty_stream"
    assert final.error.details["stop_reason"] == "other"
    assert final.error.details["semantic_event_count"] == 0
    assert set(final.error.details["stream_event_types"]) == {"message_stop", "usage"}
    assert final.metrics.token_usage.completion_tokens == 4


def test_final_decision_guard_accepts_tool_only_progress():
    """Narration is optional when a concrete tool call advances the run."""
    from app.ai.agents.planner.reliability import guard_final_decision
    from app.schemas.ai.planner import Action, PlannerDecision

    action = Action(type="tool_call", name="inspect_data", arguments={"query": "select 1"})
    decision = PlannerDecision(
        analysis_complete=False,
        plan_type="research",
        action=action,
        actions=[action],
        streaming_complete=True,
    )

    assert guard_final_decision(decision).error is None


def test_final_decision_guard_rejects_nonterminal_text_only_output():
    """Reasoning text alone cannot advance an action loop."""
    from app.ai.agents.planner.reliability import guard_final_decision
    from app.schemas.ai.planner import PlannerDecision

    decision = PlannerDecision(
        analysis_complete=False,
        assistant_message="I will inspect the data.",
        streaming_complete=True,
    )

    guarded = guard_final_decision(decision, details={"stop_reason": "max_tokens"})
    assert guarded.error is not None
    assert guarded.error.code == "no_progress"
    assert guarded.error.details == {"stop_reason": "max_tokens"}


def test_final_decision_guard_accepts_terminal_answer():
    from app.ai.agents.planner.reliability import guard_final_decision
    from app.schemas.ai.planner import PlannerDecision

    decision = PlannerDecision(
        analysis_complete=True,
        assistant_message="Done.",
        streaming_complete=True,
    )

    assert guard_final_decision(decision).error is None


def test_no_progress_budget_opens_after_two_paid_attempts():
    """The no-progress fuse is independent of the 100-step workflow budget."""
    from app.ai.agents.planner.reliability import NoProgressBudget

    budget = NoProgressBudget(max_attempts=2)
    assert budget.record() is False
    assert budget.can_retry is True
    assert budget.record() is True
    assert budget.exhausted is True


@pytest.mark.asyncio
async def test_rejected_decision_preserves_error_inside_metrics_audit():
    """Rejected attempts stay queryable without adding a schema migration."""
    from app.project_manager import ProjectManager
    from app.schemas.ai.planner import PlannerDecision, PlannerError, PlannerMetrics

    captured = {}

    async def _capture_save(_db, **kwargs):
        captured.update(kwargs)
        return kwargs

    manager = ProjectManager.__new__(ProjectManager)
    manager.save_plan_decision = _capture_save
    decision = PlannerDecision(
        analysis_complete=False,
        streaming_complete=True,
        metrics=PlannerMetrics(
            stop_reason="other",
            semantic_event_count=0,
            stream_event_types=["message_stop", "usage"],
        ),
        error=PlannerError(code="empty_stream", message="no semantic output"),
    )

    await manager.save_plan_decision_from_model(
        object(),
        agent_execution=object(),
        seq=7,
        loop_index=1,
        planner_decision_model=decision,
        phase="planner_error",
    )

    assert captured["phase"] == "planner_error"
    assert captured["metrics_json"]["planner_error"]["code"] == "empty_stream"
    assert captured["metrics_json"]["semantic_event_count"] == 0
