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
