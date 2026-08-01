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


# ── quota: provider scope, first failure, long cooldown ────────────────────

def test_breaker_trips_provider_scope_on_quota():
    """Allowance and credit balance are billed per account, not per model.

    An exhausted OpenAI key fails GPT-5 and GPT-5-mini alike, so trying the
    sibling model would only buy the same error.
    """
    br = CircuitBreaker(threshold=2, window_s=60, cooldown_s=60)
    br.record_failure("prov-1", "model-a", "quota")

    assert br.is_open("prov-1", "model-a") is True
    assert br.is_open("prov-1", "model-b") is True
    assert br.is_open("prov-2", "model-z") is False


def test_breaker_quota_trips_on_first_failure_and_holds_longer(monkeypatch):
    br = CircuitBreaker(threshold=5, window_s=600, cooldown_s=60, sticky_cooldown_s=900)
    t = [1000.0]
    monkeypatch.setattr("app.ai.llm.fallback.time.monotonic", lambda: t[0])
    br.record_failure("prov-1", "model-a", "quota")

    # One failure is proof enough — no threshold to reach.
    assert br.is_open("prov-1", "model-a") is True
    # Still shut well past the ordinary cooldown: a spent allowance doesn't
    # heal on its own, so re-probing it is pure latency.
    t[0] += 120
    assert br.is_open("prov-1", "model-a") is True
    t[0] += 800
    assert br.is_open("prov-1", "model-a") is False


def test_later_transient_failure_does_not_shorten_a_quota_trip(monkeypatch):
    br = CircuitBreaker(threshold=1, window_s=600, cooldown_s=60, sticky_cooldown_s=900)
    t = [1000.0]
    monkeypatch.setattr("app.ai.llm.fallback.time.monotonic", lambda: t[0])
    br.record_failure("prov-1", "model-a", "quota")
    br.record_failure("prov-1", "model-a", "network")

    t[0] += 120  # past the network cooldown, inside the quota one
    assert br.is_open("prov-1", "model-a") is True


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


def test_controller_force_walks_chain_for_ineligible_code(monkeypatch):
    """The loop-level rescue escalates arbitrary errors ('unknown') to a model
    switch after its retry budget is spent — force=True is that override; the
    default contract (ineligible codes never fall back) is unchanged."""
    _fresh_breaker(monkeypatch)
    primary = _model("qwen-235b", "Qwen 235B", provider_id="dgx")
    fb1 = _model("claude-haiku", "Claude Haiku", provider_id="anthropic")
    ctl = FallbackController([primary, fb1], current_model=primary)

    assert ctl.next_candidate("unknown") is None
    assert ctl.next_candidate("unknown", force=True) is fb1


def test_controller_force_does_not_trip_breaker_for_ineligible_code(monkeypatch):
    """An ineligible error says nothing about the model's availability, so a
    forced walk must not open cooldowns that other runs consult."""
    br = _fresh_breaker(monkeypatch, threshold=1)
    primary = _model("qwen-235b", "Qwen 235B", provider_id="dgx")
    fb1 = _model("claude-haiku", "Claude Haiku", provider_id="anthropic")
    ctl = FallbackController([primary, fb1], current_model=primary)

    ctl.next_candidate("unknown", force=True)
    assert br.is_open("dgx", primary.id) is False


def test_controller_force_still_records_eligible_codes(monkeypatch):
    br = _fresh_breaker(monkeypatch, threshold=1)
    primary = _model("qwen-235b", "Qwen 235B", provider_id="dgx")
    fb1 = _model("claude-haiku", "Claude Haiku", provider_id="anthropic")
    ctl = FallbackController([primary, fb1], current_model=primary)

    ctl.next_candidate("rate_limit", force=True)
    assert br.is_open("dgx", primary.id) is True


def test_controller_min_window_skips_same_size_candidates(monkeypatch):
    """Context-overflow escape: a same-size window rejects the same
    conversation, so only strictly-larger candidates are worth walking to."""
    _fresh_breaker(monkeypatch)
    primary = _model("haiku", "Haiku", provider_id="anthropic")
    primary.context_window_tokens = 200_000
    same = _model("gpt", "GPT", provider_id="openai")
    same.context_window_tokens = 200_000
    bigger = _model("sonnet", "Sonnet", provider_id="anthropic")
    bigger.context_window_tokens = 1_000_000
    ctl = FallbackController([same, bigger], current_model=primary)

    nxt = ctl.next_candidate("context_length", force=True, min_context_window=200_000)
    assert nxt is bigger


def test_controller_min_window_keeps_unknown_window_candidates(monkeypatch):
    """Unknown capability data fails open — the admin ordered the list, and a
    candidate we can't rule out beats surfacing the error unserved."""
    _fresh_breaker(monkeypatch)
    primary = _model("haiku", "Haiku", provider_id="anthropic")
    primary.context_window_tokens = 200_000
    unknown = _model("mystery", "Mystery", provider_id="custom")
    unknown.context_window_tokens = None
    ctl = FallbackController([unknown], current_model=primary)

    assert ctl.next_candidate("context_length", force=True, min_context_window=200_000) is unknown


def test_controller_window_skip_does_not_mark_attempted(monkeypatch):
    """A window-skipped candidate must stay usable for a later, non-overflow
    failure in the same run."""
    _fresh_breaker(monkeypatch)
    primary = _model("haiku", "Haiku", provider_id="anthropic")
    primary.context_window_tokens = 200_000
    same = _model("gpt", "GPT", provider_id="openai")
    same.context_window_tokens = 200_000
    ctl = FallbackController([same], current_model=primary)

    assert ctl.next_candidate("context_length", force=True, min_context_window=200_000) is None
    # Later rate-limit failure: the same-size candidate is fine for that.
    assert ctl.next_candidate("rate_limit") is same


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


def test_controller_falls_back_on_quota(monkeypatch):
    _fresh_breaker(monkeypatch)
    primary = _model("gpt-5", "GPT-5", provider_id="openai")
    other = _model("claude-sonnet", "Claude Sonnet", provider_id="anthropic")
    ctl = FallbackController([other], current_model=primary)

    assert "quota" in FALLBACK_ELIGIBLE_CODES
    assert ctl.next_candidate("quota") is other


def test_controller_skips_siblings_of_a_quota_exhausted_provider(monkeypatch):
    """The chain must not walk three models on the same spent account."""
    _fresh_breaker(monkeypatch, threshold=99)
    primary = _model("gpt-5", "GPT-5", provider_id="openai")
    sibling = _model("gpt-5-mini", "GPT-5 mini", provider_id="openai")
    elsewhere = _model("claude-sonnet", "Claude Sonnet", provider_id="anthropic")

    ctl = FallbackController([sibling, elsewhere], current_model=primary)
    # Skips gpt-5-mini: the whole OpenAI account is out of budget.
    assert ctl.next_candidate("quota") is elsewhere


def test_controller_never_returns_same_model_twice(monkeypatch):
    _fresh_breaker(monkeypatch)
    primary = _model("m1", "M1")
    fb1 = _model("m2", "M2")
    ctl = FallbackController([fb1, fb1], current_model=primary)
    assert ctl.next_candidate("provider_error") is fb1
    assert ctl.next_candidate("provider_error") is None


# ── per-user access filtering (EE llm_access_control) ─────────────────────

def _access_model(mid, name, *, restricted=False):
    m = _model(mid, name)
    m.is_restricted = restricted
    return m


@pytest.mark.asyncio
async def test_access_filter_drops_models_user_cannot_use(monkeypatch):
    from app.ai.llm.fallback import filter_chain_by_access
    import app.core.permission_resolver as resolver

    granted = _access_model("m-granted", "Granted", restricted=True)
    denied = _access_model("m-denied", "Denied", restricted=True)
    open_m = _access_model("m-open", "Open")

    async def fake_can_use(db, user_id, org_id, model):
        return model.model_id != "m-denied"

    monkeypatch.setattr(resolver, "user_can_use_model", fake_can_use)
    user = types.SimpleNamespace(id="u1")
    org = types.SimpleNamespace(id="o1")

    out = await filter_chain_by_access(None, org, user, [granted, denied, open_m])
    assert [m.model_id for m in out] == ["m-granted", "m-open"]


@pytest.mark.asyncio
async def test_access_filter_no_user_keeps_full_chain(monkeypatch):
    from app.ai.llm.fallback import filter_chain_by_access
    import app.core.permission_resolver as resolver

    called = []

    async def fake_can_use(*a, **k):
        called.append(1)
        return False

    monkeypatch.setattr(resolver, "user_can_use_model", fake_can_use)
    chain = [_access_model("m1", "M1", restricted=True)]
    out = await filter_chain_by_access(None, types.SimpleNamespace(id="o1"), None, chain)
    assert out == chain
    assert called == [], "no user context — the resolver must not be consulted"


@pytest.mark.asyncio
async def test_access_filter_error_posture(monkeypatch):
    """On a resolver failure: unrestricted models stay (failing open there
    cannot widen access), restricted models are dropped (nothing re-validates
    a fallback later, so we never risk serving a locked-down model)."""
    from app.ai.llm.fallback import filter_chain_by_access
    import app.core.permission_resolver as resolver

    async def broken(*a, **k):
        raise RuntimeError("resolver down")

    monkeypatch.setattr(resolver, "user_can_use_model", broken)
    restricted = _access_model("m-r", "Restricted", restricted=True)
    open_m = _access_model("m-o", "Open")
    user = types.SimpleNamespace(id="u1")

    out = await filter_chain_by_access(None, types.SimpleNamespace(id="o1"), user, [restricted, open_m])
    assert [m.model_id for m in out] == ["m-o"]


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
