"""Cache-read tokens must be costed at each model's own cache multiplier.

Anthropic bills a cache hit at 0.1x the base input rate on most models, but at
0.025x on Claude Fable 5.1 and Claude Mythos 5.1. The recorder applied a flat
0.1x to every Anthropic model, so a cached read on those two was reported at 4x
its real cost — and cache reads dominate a long agent run, so the console's
cost figure drifted high on the priciest models in the catalog.

The invariant under test is the multiplier itself, across the family and at
arbitrary rates and token counts — not the one model that exposed the bug.
"""
import pytest

import app.models  # noqa: F401  (register every mapper before instantiating)
from app.models.llm_model import LLMModel
from app.services.llm_usage_recorder import LLMUsageRecorderService

STANDARD = 0.1
REDUCED = 0.025


def _model(model_id: str, input_rate: float) -> LLMModel:
    return LLMModel(
        name=model_id,
        model_id=model_id,
        is_preset=True,
        is_enabled=True,
        input_cost_per_million_tokens_usd=input_rate,
    )


def _cache_read_cost(model_id: str, rate: float, tokens: int, provider: str = "anthropic") -> float:
    """Cost attributable to cache reads alone (no fresh input, no cache writes)."""
    return LLMUsageRecorderService._calc_input_cost(
        _model(model_id, rate),
        tokens=0,
        cache_read_tokens=tokens,
        provider_type=provider,
    )


@pytest.mark.parametrize(
    "model_id, expected_multiplier",
    [
        # The reduced-rate pair.
        ("claude-fable-5-1", REDUCED),
        ("claude-mythos-5-1", REDUCED),
        # Same families, previous version — deliberately still standard rate.
        # These are the ones a family-wide "fable-5"/"mythos" match would have
        # swept in, under-reporting their cost by 4x.
        ("claude-fable-5", STANDARD),
        ("claude-mythos-5", STANDARD),
        # Everything else in the catalog.
        ("claude-opus-5", STANDARD),
        ("claude-opus-4-8", STANDARD),
        ("claude-sonnet-5", STANDARD),
        ("claude-haiku-4-5-20251001", STANDARD),
        # An id we've never seen must fall back to the standard rate rather
        # than guess the cheaper one.
        ("some-custom-anthropic-deployment", STANDARD),
    ],
)
@pytest.mark.parametrize("rate, tokens", [(10.0, 640_000), (7.5, 1_234_567), (3.0, 1)])
def test_cache_read_uses_the_models_own_multiplier(model_id, expected_multiplier, rate, tokens):
    expected = (tokens / 1_000_000) * rate * expected_multiplier
    assert _cache_read_cost(model_id, rate, tokens) == pytest.approx(expected)


def test_reduced_rate_models_cost_a_quarter_of_the_standard_ones():
    """The relationship, stated independently of any absolute price."""
    rate, tokens = 10.0, 800_000
    reduced = _cache_read_cost("claude-fable-5-1", rate, tokens)
    standard = _cache_read_cost("claude-fable-5", rate, tokens)
    assert reduced == pytest.approx(standard / 4)


def test_published_per_million_cache_read_prices():
    """Anchor to the two prices Anthropic publishes, at their real base rates.

    Both families list a $10/MTok base input price, so a million cache-read
    tokens costs $0.25 on Fable 5.1 and $1.00 on Fable 5.
    """
    assert _cache_read_cost("claude-fable-5-1", 10.0, 1_000_000) == pytest.approx(0.25)
    assert _cache_read_cost("claude-fable-5", 10.0, 1_000_000) == pytest.approx(1.00)


def test_cache_multiplier_does_not_disturb_fresh_input_or_cache_writes():
    """Only the cache-read term is model-dependent; the others are unchanged."""
    rate = 10.0
    for model_id in ("claude-fable-5-1", "claude-fable-5"):
        fresh_only = LLMUsageRecorderService._calc_input_cost(
            _model(model_id, rate), tokens=500_000, provider_type="anthropic"
        )
        assert fresh_only == pytest.approx(0.5 * rate)

        writes_only = LLMUsageRecorderService._calc_input_cost(
            _model(model_id, rate),
            tokens=0,
            cache_creation_tokens=500_000,
            provider_type="anthropic",
        )
        assert writes_only == pytest.approx(0.5 * rate * 1.25)


def test_reduced_rate_is_anthropic_only():
    """A non-Anthropic provider keeps its own cache accounting.

    OpenAI/Azure report cached tokens inside prompt_tokens and charge 0.5x for
    them, so the Anthropic multiplier must not leak across providers even if a
    deployment happens to be named after a Claude model.
    """
    rate, tokens = 10.0, 400_000
    cost = LLMUsageRecorderService._calc_input_cost(
        _model("claude-fable-5-1", rate),
        tokens=tokens,
        cache_read_tokens=tokens,
        provider_type="openai",
    )
    # Full rate on prompt_tokens, less the 50% discount on the cached portion.
    assert cost == pytest.approx((tokens / 1_000_000) * rate * 0.5)


def test_missing_input_rate_costs_nothing_rather_than_raising():
    """A model with no rate on the row and none in the catalog costs 0, not a crash.

    The id must be one the catalog doesn't carry: get_input_cost_rate() falls
    back to LLM_MODEL_DETAILS when the column is NULL, so a catalogued id like
    claude-fable-5-1 would resolve to its listed price instead of no price.
    """
    model = LLMModel(name="unpriced", model_id="privately-hosted-model", is_preset=False)
    model.input_cost_per_million_tokens_usd = None
    cost = LLMUsageRecorderService._calc_input_cost(
        model, tokens=0, cache_read_tokens=1_000_000, provider_type="anthropic"
    )
    assert cost == 0.0


def test_catalog_price_is_used_when_the_row_carries_no_rate():
    """The NULL-column fallback still routes through the right multiplier."""
    model = LLMModel(name="Claude Fable 5.1", model_id="claude-fable-5-1", is_preset=True)
    model.input_cost_per_million_tokens_usd = None
    cost = LLMUsageRecorderService._calc_input_cost(
        model, tokens=0, cache_read_tokens=1_000_000, provider_type="anthropic"
    )
    # Catalog base rate $10/MTok at the reduced 0.025x cache multiplier.
    assert cost == pytest.approx(0.25)
