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
