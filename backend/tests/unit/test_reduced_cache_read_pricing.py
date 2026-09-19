"""Cache reads are not one multiplier across the Anthropic family.

Every Claude model reads cache at 0.1x the base input rate except Claude Fable
5.1 and Claude Mythos 5.1, which read at 0.025x. A single family-wide constant
therefore over-reports those two by 4x -- and cache reads dominate a long agent
run, where one report conversation replays the same system prompt, schema
context and transcript on every turn. On the most expensive models we offer,
that is the number an admin budgets against.

The mirror-image bug matters just as much and is worse: matching on the family
(``fable``, ``mythos``) instead of the version-qualified id would sweep in
Claude Fable 5 and Mythos 5, which still read at 0.1x, and UNDER-report them by
4x. Both directions are asserted below.

These go through ``cached_input_cost``/``rates_for`` -- the surface the recorder
and the cost console actually call -- rather than the tag list, so they hold for
whatever the matching is implemented with.
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest  # noqa: E402

from app.ai.llm import pricing  # noqa: E402

RATE = 10.0  # USD per million input tokens
M = 1_000_000
READS = 2_000_000

STANDARD_READ_MULTIPLIER = 0.10
REDUCED_READ_MULTIPLIER = 0.025

# Ids for the two reduced-rate models as each surface that serves them spells
# them: first-party, a dated release, a Bedrock-style id, a gateway alias.
REDUCED_RATE_IDS = [
    ("anthropic", "claude-fable-5-1"),
    ("anthropic", "claude-mythos-5-1"),
    ("anthropic", "claude-fable-5-1-20260901"),
    ("bedrock", "anthropic.claude-fable-5-1-20260901-v1:0"),
    ("vertex", "claude-mythos-5-1"),
    ("custom", "my-gateway-alias-claude-fable-5-1"),
]

# Everything else in the family, including the two the version-qualified match
# must NOT catch.
STANDARD_RATE_IDS = [
    ("anthropic", "claude-fable-5"),
    ("anthropic", "claude-mythos-5"),
    ("anthropic", "claude-opus-5"),
    ("anthropic", "claude-sonnet-5"),
    ("anthropic", "claude-haiku-4-5-20251001"),
    ("bedrock", "anthropic.claude-fable-5"),
    ("vertex", "claude-sonnet-5"),
]


def _read_cost(provider, model, reads=READS):
    return pricing.cached_input_cost(
        rate_per_million=RATE, prompt_tokens=0, cache_read_tokens=reads,
        cache_write_5m_tokens=0, cache_write_1h_tokens=0,
        provider_type=provider, model_id=model,
    )


def _expected(multiplier, reads=READS):
    return reads * RATE / M * multiplier


@pytest.mark.parametrize("provider,model", REDUCED_RATE_IDS)
def test_the_reduced_rate_models_read_cache_at_the_cheaper_multiplier(provider, model):
    assert _read_cost(provider, model) == pytest.approx(_expected(REDUCED_READ_MULTIPLIER))


@pytest.mark.parametrize("provider,model", STANDARD_RATE_IDS)
def test_every_other_claude_model_keeps_the_standard_multiplier(provider, model):
    """The inverse bug: a family-wide match would make these 4x too cheap."""
    assert _read_cost(provider, model) == pytest.approx(_expected(STANDARD_READ_MULTIPLIER))


def test_the_reduced_rate_is_a_quarter_of_the_standard_one():
    """Stated as a ratio so it survives a change to the base rate."""
    reduced = _read_cost("anthropic", "claude-fable-5-1")
    standard = _read_cost("anthropic", "claude-fable-5")
    assert reduced == pytest.approx(standard / 4)


def test_the_point_release_and_its_base_version_do_not_share_a_rate():
    """``claude-fable-5-1`` is a different model from ``claude-fable-5``, and the
    substring relationship between their ids must not merge their pricing."""
    assert _read_cost("anthropic", "claude-fable-5-1") != pytest.approx(
        _read_cost("anthropic", "claude-fable-5")
    )


@pytest.mark.parametrize("model", ["claude-fable-5-10", "claude-mythos-5-12"])
def test_a_later_point_release_does_not_inherit_the_reduced_rate(model):
    """A version-prefix match would hand 5.10 the 5.1 rate. Unknown ids take the
    standard multiplier: over-reporting gets questioned, under-reporting gets
    budgeted against."""
    assert _read_cost("anthropic", model) == pytest.approx(_expected(STANDARD_READ_MULTIPLIER))


def test_an_unknown_claude_id_defaults_to_the_standard_multiplier():
    assert _read_cost("anthropic", "claude-something-unreleased") == pytest.approx(
        _expected(STANDARD_READ_MULTIPLIER)
    )


@pytest.mark.parametrize("provider,model", REDUCED_RATE_IDS)
def test_cache_writes_are_untouched_by_the_reduced_read_rate(provider, model):
    """Only reads moved. Writes stay 1.25x/2x on these models, so a change that
    reached the write path would be a new mispricing, not a fix."""
    writes = 1_000_000
    five_m = pricing.cached_input_cost(
        rate_per_million=RATE, prompt_tokens=0, cache_read_tokens=0,
        cache_write_5m_tokens=writes, cache_write_1h_tokens=0,
        provider_type=provider, model_id=model,
    )
    one_h = pricing.cached_input_cost(
        rate_per_million=RATE, prompt_tokens=0, cache_read_tokens=0,
        cache_write_5m_tokens=0, cache_write_1h_tokens=writes,
        provider_type=provider, model_id=model,
    )
    assert five_m == pytest.approx(writes * RATE / M * 1.25)
    assert one_h == pytest.approx(writes * RATE / M * 2.00)


@pytest.mark.parametrize("provider,model", REDUCED_RATE_IDS + STANDARD_RATE_IDS)
def test_uncached_input_is_unaffected_across_the_family(provider, model):
    """The reduced rate applies to cached reads only; base input is the base
    input on every model."""
    cost = pricing.cached_input_cost(
        rate_per_million=RATE, prompt_tokens=100_000, cache_read_tokens=0,
        cache_write_5m_tokens=0, cache_write_1h_tokens=0,
        provider_type=provider, model_id=model,
    )
    assert cost == pytest.approx(100_000 * RATE / M)


@pytest.mark.parametrize("provider,model", REDUCED_RATE_IDS)
def test_reads_still_add_to_the_bill_rather_than_discounting_it(provider, model):
    """Anthropic reports cache reads outside prompt_tokens on these models too,
    so the cheaper rate must not flip the sign of the term."""
    assert _read_cost(provider, model) > 0
