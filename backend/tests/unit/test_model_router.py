"""Unit tests for auto model routing pure logic (app.ai.model_router).

Covers the parts that carry no DB dependency: the per-request tool schema, the
RoutingController escalation contract (which is the mechanism that propagates a
routed model to every later planner turn and tool call), and the savings math.
Candidate resolution and the completion-service decision are covered by the e2e
suite (tests/e2e/rbac/test_auto_model_routing.py).
"""
import types

import pytest

from app.ai.model_router import (
    build_route_model_schema,
    compute_routing_savings_usd,
    get_routing_hint,
    RoutingController,
)


def _model(mid, name, *, small=False, default=False, hint=None, in_rate=1.0, out_rate=6.0, db_id=None):
    """A lightweight stand-in with the attributes the pure functions read."""
    m = types.SimpleNamespace()
    m.id = db_id or mid
    m.model_id = mid
    m.name = name
    m.is_small_default = small
    m.is_default = default
    m.config = {"routing_hint": hint} if hint else {}
    m.get_input_cost_rate = lambda: in_rate
    m.get_output_cost_rate = lambda: out_rate
    return m


class _FakeAgent:
    """Minimal agent whose model swap mirrors AgentV2._apply_routed_model.

    Propagation in the real agent works because runtime_ctx is rebuilt from
    ``self.model`` on every tool dispatch — so proving the controller swaps
    ``agent.model`` proves the propagation mechanism.
    """

    def __init__(self, model):
        self.model = model
        self.planner_llm_rebuilt_for = None

    def _apply_routed_model(self, model):
        self.model = model
        self.planner_llm_rebuilt_for = model.model_id


# ── tool schema ────────────────────────────────────────────────────────────

def test_schema_enum_lists_only_given_candidates_with_hints_in_description():
    small = _model("gpt-small", "GPT Small", small=True, hint="simple lookups and follow-ups")
    big = _model("gpt-big", "GPT Big", default=True, hint="multi-source dashboards")
    schema = build_route_model_schema([small, big], current_model_id=small.id)

    enum = schema["properties"]["model"]["enum"]
    assert enum == ["gpt-small", "gpt-big"]
    desc = schema["properties"]["model"]["description"]
    # Admin guidance is surfaced verbatim so the planner routes on intent.
    assert "simple lookups and follow-ups" in desc
    assert "multi-source dashboards" in desc
    # The model cannot name anything off the enum.
    assert schema["properties"]["model"]["type"] == "string"
    assert schema["required"] == ["model"]


def test_get_routing_hint_reads_config_and_ignores_blank():
    assert get_routing_hint(_model("m", "M", hint="use for X")) == "use for X"
    assert get_routing_hint(_model("m", "M", hint="   ")) is None
    assert get_routing_hint(_model("m", "M")) is None


# ── controller escalation (propagation mechanism) ──────────────────────────

@pytest.mark.asyncio
async def test_controller_applies_valid_escalation_and_swaps_agent_model():
    small = _model("gpt-small", "GPT Small", small=True, hint="simple", db_id="s1")
    big = _model("gpt-big", "GPT Big", default=True, hint="complex", db_id="b1")
    agent = _FakeAgent(small)
    ctrl = RoutingController(agent, [small, big])

    obs = await ctrl.apply("gpt-big", "needs a dashboard")

    assert obs["routed"] is True
    assert obs["model"] == "gpt-big"
    # The swap is what later tool calls inherit via runtime_ctx["model"].
    assert agent.model is big
    assert agent.planner_llm_rebuilt_for == "gpt-big"
    assert ctrl.escalated is True


@pytest.mark.asyncio
async def test_controller_matches_by_db_id_and_name_too():
    small = _model("gpt-small", "GPT Small", small=True, hint="simple", db_id="s1")
    big = _model("gpt-big", "GPT Big", default=True, hint="complex", db_id="b1")
    for ref in ("b1", "GPT Big"):
        agent = _FakeAgent(small)
        ctrl = RoutingController(agent, [small, big])
        obs = await ctrl.apply(ref, None)
        assert obs["routed"] is True and agent.model is big


@pytest.mark.asyncio
async def test_controller_rejects_unknown_model_and_leaves_agent_unchanged():
    small = _model("gpt-small", "GPT Small", small=True, hint="simple", db_id="s1")
    big = _model("gpt-big", "GPT Big", default=True, hint="complex", db_id="b1")
    agent = _FakeAgent(small)
    ctrl = RoutingController(agent, [small, big])

    obs = await ctrl.apply("gpt-not-a-real-target", "sneaky")

    assert obs["routed"] is False
    assert obs["error"]["code"] == "invalid_model"
    assert agent.model is small  # unchanged
    assert ctrl.escalated is False


@pytest.mark.asyncio
async def test_controller_noops_when_already_on_target():
    small = _model("gpt-small", "GPT Small", small=True, hint="simple", db_id="s1")
    agent = _FakeAgent(small)
    ctrl = RoutingController(agent, [small])
    obs = await ctrl.apply("gpt-small", None)
    assert obs["routed"] is False
    assert agent.model is small


# ── effective-model persistence (the answer's model badge) ─────────────────

def test_apply_routed_model_stamps_effective_model_on_completion():
    """Escalating must write the new model onto the system completion.

    The reports view badges each answer with ``completion.model``; without this
    write a routed run would keep showing the small model it *started* on even
    after the planner escalated and the stronger model did the work.
    """
    from app.ai.agent_v2 import AgentV2

    small = _model("gpt-small", "GPT Small", small=True, db_id="s1")
    big = _model("gpt-big", "GPT Big", default=True, db_id="b1")

    completion = types.SimpleNamespace(model="gpt-small")
    added: list = []
    fake_self = types.SimpleNamespace(
        model=small,
        _routing_escalated=False,
        system_completion=completion,
        db=types.SimpleNamespace(add=added.append),
        planner=types.SimpleNamespace(llm=None),
        usage_limit_context=None,
    )

    # Call the real method against a light stand-in (avoids full AgentV2 init).
    AgentV2._apply_routed_model(fake_self, big)

    assert fake_self.model is big
    assert fake_self._routing_escalated is True
    assert completion.model == "gpt-big", "completion must carry the escalated model_id"
    assert completion in added, "the change must be staged on the session for commit"


def test_apply_routed_model_without_completion_does_not_raise():
    """A run with no system completion (e.g. some eval paths) still swaps safely."""
    from app.ai.agent_v2 import AgentV2

    small = _model("gpt-small", "GPT Small", small=True, db_id="s1")
    big = _model("gpt-big", "GPT Big", default=True, db_id="b1")
    fake_self = types.SimpleNamespace(
        model=small,
        _routing_escalated=False,
        system_completion=None,
        db=types.SimpleNamespace(add=lambda o: None),
        planner=types.SimpleNamespace(llm=None),
        usage_limit_context=None,
    )
    AgentV2._apply_routed_model(fake_self, big)
    assert fake_self.model is big


# ── savings math ───────────────────────────────────────────────────────────

def _usage(routed, baseline_id, prompt, completion, total_cost, cache_read=0, cache_creation=0):
    return types.SimpleNamespace(
        routed=routed, baseline_model_id=baseline_id,
        prompt_tokens=prompt, completion_tokens=completion,
        cache_read_tokens=cache_read, cache_creation_tokens=cache_creation,
        total_cost_usd=total_cost,
    )


def test_savings_is_baseline_priced_tokens_minus_actual_over_routed_records():
    # Baseline (default) model: $10/M in, $30/M out.
    rates = {"base": {"in": 10.0, "out": 30.0}}
    # A small-model call: 1M in, 1M out, actually cost $2 total.
    rec = _usage(True, "base", prompt=1_000_000, completion=1_000_000, total_cost=2.0)
    # Baseline would have cost 10 + 30 = 40; saved = 40 - 2 = 38.
    assert compute_routing_savings_usd([rec], rates) == pytest.approx(38.0)


def test_savings_ignores_non_routed_and_missing_baseline_records():
    rates = {"base": {"in": 10.0, "out": 30.0}}
    not_routed = _usage(False, "base", 1_000_000, 0, 1.0)
    no_baseline = _usage(True, None, 1_000_000, 0, 1.0)
    assert compute_routing_savings_usd([not_routed, no_baseline], rates) == 0.0


def test_savings_nets_escalation_overhead_negative_when_actual_exceeds_baseline():
    # An escalated call that ran ON a model pricier than the baseline default
    # contributes negative savings — the KPI is honest/net.
    rates = {"base": {"in": 1.0, "out": 1.0}}
    rec = _usage(True, "base", prompt=1_000_000, completion=0, total_cost=5.0)
    # baseline = $1; actual = $5 → -4.
    assert compute_routing_savings_usd([rec], rates) == pytest.approx(-4.0)
