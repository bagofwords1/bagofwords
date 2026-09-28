"""Cached tokens must price by model family and TTL, not by provider account.

Three routes were mispriced at once because the arithmetic branched on
``LLMProvider.provider_type``: Claude on Vertex matched no branch and billed
cached tokens at $0, Claude on Azure Foundry took the OpenAI branch and had a
50% rebate subtracted from a cost that never included those tokens, and Bedrock
and Gemini had no branch at all. Separately, a 1-hour cache write bills at 2x
where a 5-minute one bills at 1.25x, so collapsing the two understates spend
wherever the longer TTL is in use.

The invariants below are written against the pricing surface rather than any
one provider string, so they hold for routes nobody has added yet.
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest  # noqa: E402

from app.ai.llm import pricing  # noqa: E402

RATE = 3.0  # USD per million input tokens; any positive rate works
M = 1_000_000

# Every surface that can serve Claude. The provider account differs; the
# billing must not.
CLAUDE_ROUTES = [
    ("anthropic", "claude-haiku-4-5-20251001"),
    ("vertex", "claude-haiku-4-5"),
    ("azure", "claude-haiku-4-5"),
    ("bedrock", "anthropic.claude-sonnet-5"),
    ("custom", "my-gateway-alias-claude-sonnet-5"),
    ("openai", "claude-sonnet-5"),  # Claude behind an OpenAI-compatible gateway
]


def _cost(provider, model, prompt=0, read=0, w5=0, w1=0):
    return pricing.cached_input_cost(
        rate_per_million=RATE, prompt_tokens=prompt, cache_read_tokens=read,
        cache_write_5m_tokens=w5, cache_write_1h_tokens=w1,
        provider_type=provider, model_id=model,
    )


@pytest.mark.parametrize("provider,model", CLAUDE_ROUTES)
def test_claude_prices_the_same_on_every_route(provider, model):
    """The account that serves Claude does not change what Claude costs."""
    assert pricing.resolve_family(provider, model) == pricing.ANTHROPIC
    assert _cost(provider, model, read=100_000) == pytest.approx(100_000 * RATE / M * 0.10)


@pytest.mark.parametrize("provider,model", CLAUDE_ROUTES)
def test_cached_tokens_are_never_free_on_any_claude_route(provider, model):
    """The Vertex bug: cached tokens silently costing nothing."""
    assert _cost(provider, model, read=50_000) > 0
    assert _cost(provider, model, w5=50_000) > 0
    assert _cost(provider, model, w1=50_000) > 0


@pytest.mark.parametrize("provider,model", CLAUDE_ROUTES)
def test_cache_reads_add_to_claude_cost_rather_than_discounting_it(provider, model):
    """The Azure bug: an OpenAI-style rebate applied to an Anthropic response.

    Anthropic reports cache reads OUTSIDE prompt_tokens, so a read can only
    ever increase the bill. Subtracting made a cached-heavy call look cheaper
    than the same call with no cache at all.
    """
    uncached_only = _cost(provider, model, prompt=10_000)
    with_reads = _cost(provider, model, prompt=10_000, read=100_000)
    assert with_reads > uncached_only


def test_one_hour_writes_cost_more_than_five_minute_writes():
    """The regression this file was written for: one rate for two TTLs."""
    five = _cost("anthropic", "claude-haiku-4-5", w5=100_000)
    hour = _cost("anthropic", "claude-haiku-4-5", w1=100_000)
    assert hour > five
    assert hour == pytest.approx(five * (2.00 / 1.25))


def test_a_write_with_no_ttl_split_is_billed_at_the_cheaper_rate():
    """Never over-bill on missing data: an unattributed write is 5-minute."""
    unsplit = _cost("anthropic", "claude-haiku-4-5", w5=100_000)
    assert unsplit < _cost("anthropic", "claude-haiku-4-5", w1=100_000)


def test_openai_cached_tokens_are_a_rebate_not_an_addition():
    """OpenAI reports cached tokens INSIDE prompt_tokens, already at full rate."""
    full = _cost("openai", "gpt-5", prompt=100_000)
    half_cached = _cost("openai", "gpt-5", prompt=100_000, read=50_000)
    assert half_cached < full
    # 50k of the 100k billed at half price.
    assert half_cached == pytest.approx((50_000 + 50_000 * 0.5) * RATE / M)


def test_cost_never_goes_negative():
    """A provider reporting more cached tokens than prompt tokens must not
    produce a negative bill that silently offsets other calls."""
    assert _cost("openai", "gpt-5", prompt=10, read=10_000_000) >= 0.0


def test_a_zero_rate_model_costs_nothing():
    assert pricing.cached_input_cost(
        rate_per_million=0.0, prompt_tokens=1_000, cache_read_tokens=1_000,
        cache_write_5m_tokens=1_000, cache_write_1h_tokens=1_000,
        provider_type="anthropic", model_id="claude-haiku-4-5",
    ) == 0.0


# --- hit rate -------------------------------------------------------------


def test_hit_rate_denominator_follows_the_family():
    """Anthropic reports cached tokens outside prompt_tokens; OpenAI inside.
    One formula for both would misreport one of them."""
    anthropic = pricing.cache_hit_rate(
        prompt_tokens=1_000, cache_read_tokens=9_000, cache_creation_tokens=0,
        provider_type="anthropic", model_id="claude-haiku-4-5",
    )
    openai = pricing.cache_hit_rate(
        prompt_tokens=10_000, cache_read_tokens=9_000, cache_creation_tokens=0,
        provider_type="openai", model_id="gpt-5",
    )
    # Same underlying call: 9k of 10k input tokens served from cache.
    assert anthropic == pytest.approx(0.9)
    assert openai == pytest.approx(0.9)


@pytest.mark.parametrize("provider,model", [
    ("google", "gemini-2.5-pro"),
    ("vertex", "gemini-2.5-pro"),
    ("bedrock", "amazon.nova-pro-v1:0"),
])
def test_families_without_cache_telemetry_report_none_not_zero(provider, model):
    """0% and "we cannot see it" are different answers. Rendering the second as
    the first is a false alarm at best and false comfort at worst."""
    assert pricing.cache_hit_rate(
        prompt_tokens=10_000, cache_read_tokens=0, cache_creation_tokens=0,
        provider_type=provider, model_id=model,
    ) is None


def test_hit_rate_is_none_when_there_was_no_input():
    assert pricing.cache_hit_rate(
        prompt_tokens=0, cache_read_tokens=0, cache_creation_tokens=0,
        provider_type="anthropic", model_id="claude-haiku-4-5",
    ) is None


def test_hit_rate_is_bounded_to_one():
    """A provider over-reporting reads must not produce >100%."""
    rate = pricing.cache_hit_rate(
        prompt_tokens=10, cache_read_tokens=10_000_000, cache_creation_tokens=0,
        provider_type="openai", model_id="gpt-5",
    )
    assert rate == pytest.approx(1.0)


# --- per-model read rates -------------------------------------------------
#
# The family rate is the rule; a few models are exceptions. Claude Fable 5.1
# and Claude Mythos 5.1 bill a cache hit at 0.025x base input where the rest
# of the family pays 0.1x, so charging them the family rate overstates every
# cached read on them by 4x. These assert the exception applies where it
# should and, just as importantly, nowhere else.

REDUCED_READ_MODELS = ["claude-fable-5-1", "claude-mythos-5-1"]
# Same families one version back, plus the rest of the lineup: all still 0.1x.
# These are what a family-wide "fable-5"/"mythos" match would have swept in,
# under-reporting their cost by 4x — the same bug reversed.
STANDARD_READ_MODELS = [
    "claude-fable-5", "claude-mythos-5", "claude-opus-5",
    "claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5-20251001",
]


@pytest.mark.parametrize("model", REDUCED_READ_MODELS)
def test_reduced_rate_models_read_at_a_quarter_of_the_family_rate(model):
    assert pricing.rates_for("anthropic", model).read == pytest.approx(0.025)
    assert _cost("anthropic", model, read=100_000) == pytest.approx(
        100_000 * RATE / M * 0.025
    )


@pytest.mark.parametrize("model", STANDARD_READ_MODELS)
def test_every_other_claude_keeps_the_family_read_rate(model):
    assert pricing.rates_for("anthropic", model).read == pytest.approx(0.10)


@pytest.mark.parametrize("model", REDUCED_READ_MODELS)
def test_the_reduced_rate_follows_the_model_across_every_route(model):
    """It is a property of the model, not of the account serving it."""
    for provider in ("anthropic", "vertex", "bedrock", "azure", "custom"):
        assert pricing.rates_for(provider, f"{provider}.{model}").read == pytest.approx(0.025)


@pytest.mark.parametrize("model", REDUCED_READ_MODELS)
def test_only_reads_are_reduced_writes_keep_the_family_rates(model):
    """Anthropic publishes the same 1.25x / 2x write rates for these models."""
    rates = pricing.rates_for("anthropic", model)
    assert rates.write_5m == pytest.approx(1.25)
    assert rates.write_1h == pytest.approx(2.00)


def test_an_unknown_claude_id_keeps_the_family_rate():
    """Unpriced models default to the standard rate, not the cheaper one."""
    assert pricing.rates_for("anthropic", "claude-something-unreleased").read == pytest.approx(0.10)


def test_the_reduced_rate_never_leaks_into_an_openai_shaped_rebate():
    """`read` means 'additive cost' for Anthropic but 'cached price' behind a
    rebate for OpenAI. A 0.025 leaking across would compute a 97.5% refund."""
    assert pricing.rates_for("openai", "gpt-5-fable-5-1-lookalike").read == pytest.approx(0.50)
    full = _cost("openai", "gpt-5-fable-5-1-lookalike", prompt=100_000)
    cached = _cost("openai", "gpt-5-fable-5-1-lookalike", prompt=100_000, read=50_000)
    assert cached == pytest.approx((50_000 + 50_000 * 0.5) * RATE / M)
    assert cached < full


def test_published_per_million_cache_read_prices():
    """Anchored to the two prices Anthropic publishes, at their real $10 base."""
    def at_ten(model):
        return pricing.cached_input_cost(
            rate_per_million=10.0, prompt_tokens=0, cache_read_tokens=M,
            cache_write_5m_tokens=0, cache_write_1h_tokens=0,
            provider_type="anthropic", model_id=model,
        )
    assert at_ten("claude-fable-5-1") == pytest.approx(0.25)
    assert at_ten("claude-fable-5") == pytest.approx(1.00)


def test_opus_5_5_reads_at_its_published_rate_and_opus_5_does_not():
    """Claude Opus 5.5 publishes $0.20/MTok cache reads on a $4 base (0.05x);
    Claude Opus 5 stays at the family 0.1x."""
    assert pricing.rates_for("anthropic", "claude-opus-5-5").read == pytest.approx(0.05)
    assert pricing.rates_for("bedrock", "anthropic.claude-opus-5-5").read == pytest.approx(0.05)
    assert pricing.rates_for("anthropic", "claude-opus-5").read == pytest.approx(0.10)
    at_four = pricing.cached_input_cost(
        rate_per_million=4.0, prompt_tokens=0, cache_read_tokens=M,
        cache_write_5m_tokens=0, cache_write_1h_tokens=0,
        provider_type="anthropic", model_id="claude-opus-5-5",
    )
    assert at_four == pytest.approx(0.20)
