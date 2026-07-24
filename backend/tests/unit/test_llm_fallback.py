"""Unit tests for LLM fallback pure logic (app.ai.llm.fallback).

Covers the circuit breaker's two trip scopes (model vs provider), the
per-run FallbackController walk (eligibility codes, attempted-set, breaker
skips, chain exhaustion), and the settings-order reader. Chain resolution
against the DB and the full agent swap are covered by the sandbox e2e flow.
"""
import types

import pytest

from app.ai.llm.fallback import (
    CircuitBreaker,
    FALLBACK_ELIGIBLE_CODES,
    FallbackController,
    get_fallback_order,
)


def _model(mid, name, *, db_id=None, provider_id="prov-1"):
    m = types.SimpleNamespace()
    m.id = db_id or mid
    m.model_id = mid
    m.name = name
    m.provider = types.SimpleNamespace(id=provider_id, provider_type="custom")
    return m


# ── circuit breaker ────────────────────────────────────────────────────────

def test_breaker_trips_model_scope_on_rate_limit_only_for_that_model():
    br = CircuitBreaker(threshold=2, window_s=60, cooldown_s=60)
    br.record_failure("prov-1", "model-a", "rate_limit")
    br.record_failure("prov-1", "model-a", "rate_limit")

    assert br.is_open("prov-1", "model-a") is True
    # Same provider, different model — Claude Haiku is not blocked by an
    # Opus 429. This is the same-provider-fallback contract.
    assert br.is_open("prov-1", "model-b") is False


def test_breaker_trips_provider_scope_on_network_for_all_models():
    br = CircuitBreaker(threshold=2, window_s=60, cooldown_s=60)
    br.record_failure("prov-1", "model-a", "network")
    br.record_failure("prov-1", "model-b", "network")

    # Both models on the provider are blocked — the endpoint is unreachable.
    assert br.is_open("prov-1", "model-a") is True
    assert br.is_open("prov-1", "model-c") is True
    # A different provider is untouched.
    assert br.is_open("prov-2", "model-z") is False


def test_breaker_below_threshold_stays_closed():
    br = CircuitBreaker(threshold=3, window_s=60, cooldown_s=60)
    br.record_failure("prov-1", "model-a", "rate_limit")
    br.record_failure("prov-1", "model-a", "rate_limit")
    assert br.is_open("prov-1", "model-a") is False


def test_breaker_cooldown_elapses(monkeypatch):
    br = CircuitBreaker(threshold=1, window_s=60, cooldown_s=10)
    t = [1000.0]
    monkeypatch.setattr("app.ai.llm.fallback.time.monotonic", lambda: t[0])
    br.record_failure("prov-1", "model-a", "rate_limit")
    assert br.is_open("prov-1", "model-a") is True
    t[0] += 11
    assert br.is_open("prov-1", "model-a") is False


# ── fallback controller ────────────────────────────────────────────────────

def _fresh_breaker(monkeypatch, **kw):
    """Swap the module singleton so tests don't leak state into each other."""
    import app.ai.llm.fallback as fb
    br = CircuitBreaker(**{"threshold": 99, "window_s": 60, "cooldown_s": 60, **kw})
    monkeypatch.setattr(fb, "breaker", br)
    return br


def test_controller_walks_chain_in_order_and_skips_current(monkeypatch):
    _fresh_breaker(monkeypatch)
    primary = _model("qwen-235b", "Qwen 235B", provider_id="dgx")
    fb1 = _model("qwen-30b", "Qwen 30B", provider_id="dgx")
    fb2 = _model("claude-haiku", "Claude Haiku", provider_id="anthropic")

    # Chain includes the primary itself (admin listed it first) — it must be
    # skipped because it is the currently-failing model.
    ctl = FallbackController([primary, fb1, fb2], current_model=primary)

    nxt = ctl.next_candidate("rate_limit")
    assert nxt is fb1
    nxt = ctl.next_candidate("rate_limit")
    assert nxt is fb2
    # Exhausted.
    assert ctl.next_candidate("rate_limit") is None


def test_controller_ignores_non_eligible_codes(monkeypatch):
    _fresh_breaker(monkeypatch)
    primary = _model("m1", "M1")
    other = _model("m2", "M2")
    ctl = FallbackController([other], current_model=primary)

    for code in ("auth", "context_length", "unknown", ""):
        assert code not in FALLBACK_ELIGIBLE_CODES
        assert ctl.next_candidate(code) is None
    # Still eligible afterwards — non-eligible codes must not consume the chain.
    assert ctl.next_candidate("rate_limit") is other


def test_controller_skips_breaker_open_candidates(monkeypatch):
    br = _fresh_breaker(monkeypatch, threshold=1)
    primary = _model("m1", "M1", provider_id="p1")
    dead = _model("m2", "M2", provider_id="p2")
    alive = _model("m3", "M3", provider_id="p3")
    # p2 is already known-unreachable.
    br.record_failure("p2", "m2", "network")

    ctl = FallbackController([dead, alive], current_model=primary)
    assert ctl.next_candidate("rate_limit") is alive


def test_controller_records_failure_for_failing_model(monkeypatch):
    br = _fresh_breaker(monkeypatch, threshold=1)
    primary = _model("m1", "M1", provider_id="p1")
    other = _model("m2", "M2", provider_id="p2")
    ctl = FallbackController([other], current_model=primary)

    ctl.next_candidate("rate_limit")
    # The failing model tripped its own (model-scope) breaker; provider stays
    # usable for sibling models.
    assert br.is_open("p1", "m1") is True
    assert br.is_open("p1", "m1-sibling") is False


def test_controller_never_returns_same_model_twice(monkeypatch):
    _fresh_breaker(monkeypatch)
    primary = _model("m1", "M1")
    fb1 = _model("m2", "M2")
    ctl = FallbackController([fb1, fb1], current_model=primary)
    assert ctl.next_candidate("provider_error") is fb1
    assert ctl.next_candidate("provider_error") is None


# ── settings order reader ──────────────────────────────────────────────────

class _Settings:
    def __init__(self, value):
        self._value = value

    def get_config(self, key, default=None):
        return self._value if key == "llm_fallback_order" else default


def test_get_fallback_order_reads_bare_list():
    assert get_fallback_order(_Settings(["a", "b"])) == ["a", "b"]


def test_get_fallback_order_tolerates_garbage():
    assert get_fallback_order(_Settings(None)) == []
    assert get_fallback_order(_Settings("nope")) == []
    assert get_fallback_order(_Settings([1, None, "ok", ""])) == ["ok"]
    assert get_fallback_order(None) == []


def test_get_fallback_order_caps_length():
    assert len(get_fallback_order(_Settings([f"m{i}" for i in range(50)]))) == 10
