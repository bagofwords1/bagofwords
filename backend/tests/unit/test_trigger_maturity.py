"""Maturity gating for knowledge-harness triggers.

Contract under test (suggest_instructions/trigger.py):
- AMBIENT conditions (clarify→create, retry recovery, user code) are skipped
  when every agent in the session is production-grade ("ok").
- Human-taught conditions (explicit correction, failed-then-fixed, MCP
  contract discovery) run at every maturity.
- Condition F (inspect_then_create_data) is disabled outright — evaluate()
  must not call it at ANY maturity.
- evaluate() surfaces session_maturity so the harness can flip to the
  edit-first posture (prompt JIT block + halved step budget).

All _check_* methods and the maturity resolver are stubbed — this tests the
gating wiring, not the individual condition queries.
"""
import pytest

from app.ai.agents.suggest_instructions.trigger import (
    AMBIENT_CONDITIONS,
    InstructionTriggerEvaluator,
    TriggerCondition,
)


class _Settings:
    """Org settings stub: both gates on."""

    def get_config(self, key):
        return None  # None means "default on" for both gates


def _evaluator():
    return InstructionTriggerEvaluator(
        db=None,
        organization_settings=_Settings(),
        report_id="r1",
        current_execution_id="e1",
        user_message="hello",
        mode="chat",
    )


def _stub_conditions(monkeypatch, ev, maturity, called, call_kwargs=None):
    """Stub every condition check to 'met' and record which ones ran."""

    def make(name):
        async def check(*args, **kwargs):
            called.add(name)
            if call_kwargs is not None:
                call_kwargs[name] = kwargs
            return TriggerCondition(name=name, hint=name, met=True)
        return check

    monkeypatch.setattr(ev, "_check_clarify_then_create_data", make("clarify_then_create_data"))
    monkeypatch.setattr(ev, "_check_retry_recovery", make("retry_recovery"))
    monkeypatch.setattr(ev, "_check_user_provided_code", make("user_provided_code"))
    monkeypatch.setattr(ev, "_check_user_explicit_correction", make("user_explicit_correction"))
    monkeypatch.setattr(ev, "_check_failed_then_fixed", make("failed_then_fixed"))
    monkeypatch.setattr(ev, "_check_inspect_then_create_data", make("inspect_then_create_data"))
    monkeypatch.setattr(ev, "_check_mcp_failed_then_fixed", make("mcp_failed_then_fixed"))
    monkeypatch.setattr(ev, "_check_positive_feedback_with_create_data", make("positive_feedback_create_data"))

    async def maturity_stub():
        return maturity
    monkeypatch.setattr(ev, "_resolve_session_maturity", maturity_stub)


def test_ambient_set_is_exactly_the_self_generated_signals():
    assert AMBIENT_CONDITIONS == {
        "clarify_then_create_data",
        "retry_recovery",
        "user_provided_code",
    }


@pytest.mark.asyncio
async def test_training_agent_runs_all_active_conditions(monkeypatch):
    ev = _evaluator()
    called = set()
    kwargs = {}
    _stub_conditions(monkeypatch, ev, "training", called, kwargs)
    result = await ev.evaluate()
    names = {c["name"] for c in result["conditions"]}
    assert result["decision"] is True
    assert result["session_maturity"] == "training"
    assert AMBIENT_CONDITIONS <= names
    assert {"user_explicit_correction", "failed_then_fixed", "mcp_failed_then_fixed"} <= names
    # C runs in STANDARD mode on training: a prior turn is required.
    assert kwargs["user_explicit_correction"] == {"require_prior_turn": True}
    # F is disabled at every maturity — never even called.
    assert "inspect_then_create_data" not in called
    assert "inspect_then_create_data" not in names


@pytest.mark.asyncio
async def test_prod_agent_skips_ambient_and_corrections(monkeypatch):
    ev = _evaluator()
    called = set()
    _stub_conditions(monkeypatch, ev, "ok", called)
    result = await ev.evaluate()
    names = {c["name"] for c in result["conditions"]}
    assert result["session_maturity"] == "ok"
    # Ambient conditions neither ran nor reported.
    assert not (AMBIENT_CONDITIONS & called)
    assert not (AMBIENT_CONDITIONS & names)
    # C is OFF on prod — never even called.
    assert "user_explicit_correction" not in called
    assert "user_explicit_correction" not in names
    # Failure-fixed + user-initiated conditions survive.
    assert {"failed_then_fixed", "mcp_failed_then_fixed",
            "positive_feedback_create_data"} <= names
    assert result["decision"] is True


@pytest.mark.asyncio
async def test_development_agent_keeps_full_sensitivity_with_aggressive_c(monkeypatch):
    ev = _evaluator()
    called = set()
    kwargs = {}
    _stub_conditions(monkeypatch, ev, "development", called, kwargs)
    result = await ev.evaluate()
    names = {c["name"] for c in result["conditions"]}
    assert AMBIENT_CONDITIONS <= names
    assert result["session_maturity"] == "development"
    # C runs in AGGRESSIVE mode on development: any turn qualifies.
    assert kwargs["user_explicit_correction"] == {"require_prior_turn": False}


# --- keyword pruning: spec vocabulary must not read as correction -----------

def _matches(msg: str) -> bool:
    from app.ai.agents.suggest_instructions.trigger import CORRECTION_KEYWORDS
    m = msg.lower()
    return any(kw in m for kw in CORRECTION_KEYWORDS)


def test_removed_keywords_no_longer_match_ordinary_asks():
    # The observed false positive: turn-1 ask waking the harness.
    assert not _matches("Identify the top 10 customers with no purchases")
    assert not _matches("show the error rate by region")
    assert not _matches("the table should be sorted by revenue")
    assert not _matches("revenue by country rather than city")


def test_kept_keywords_still_match_real_corrections():
    assert _matches("no, I meant net revenue")
    assert _matches("actually, exclude cancelled invoices")
    assert _matches("that's wrong — use invoice totals")
    assert _matches("use billing country instead")
