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


def test_fault_injector_waits_for_min_index(monkeypatch):
    """Faults fire mid-run (after at least one real step), never at index 0 —
    that's what makes the sandbox scenario 'an error mid multi-step run'."""
    monkeypatch.setattr(agent_v2, "_LOOP_FAULT_BUDGET", 1)
    monkeypatch.setattr(agent_v2, "_LOOP_FAULT_MIN_INDEX", 1)

    agent_v2._maybe_inject_loop_fault(0)  # below min index: no fault
    assert agent_v2._LOOP_FAULT_BUDGET == 1
    with pytest.raises(RuntimeError):
        agent_v2._maybe_inject_loop_fault(1)
