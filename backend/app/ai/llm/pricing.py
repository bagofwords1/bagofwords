"""Cached-token pricing, keyed on the model family rather than the account.

Cache rates are a property of the model FAMILY, not of the provider account that
serves it. Claude bills cache reads at 0.1x the input rate and cache writes at
1.25x (5-minute TTL) or 2x (1-hour TTL) whether it is reached through the
first-party API, Google Vertex, Azure AI Foundry or Amazon Bedrock. Pricing on
``LLMProvider.provider_type`` therefore got three routes wrong at once:

  * Claude on Vertex (``provider_type == "vertex"``) matched no branch at all,
    so every cached token was priced at $0;
  * Claude on Azure Foundry (``provider_type == "azure"``) took the OpenAI
    branch and *subtracted* a 50% discount from a cost that never included
    those tokens, because Anthropic excludes cache reads from ``input_tokens``;
  * Claude on Bedrock and Gemini had no branch at all.

The second axis is how a provider REPORTS cached tokens, which decides whether
they must be added to the bill or are already inside it:

  * Anthropic-family responses report ``cache_read_input_tokens`` and
    ``cache_creation_input_tokens`` ALONGSIDE ``input_tokens`` — the cached
    tokens are excluded from the uncached count, so each is billed separately.
  * OpenAI-family responses report ``prompt_tokens_details.cached_tokens`` as a
    SUBSET of ``prompt_tokens`` — the cached tokens are already billed at full
    rate, so the discount is applied as a rebate.

Getting that distinction wrong is the single most common cause of a cost
console that disagrees with the provider's invoice, so it is modelled
explicitly here rather than left to a per-branch comment.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Optional

# Model families. The name is the billing behavior, not the vendor: a Claude
# model served by Bedrock is ANTHROPIC here because that is how its tokens price.
ANTHROPIC = "anthropic"
OPENAI = "openai"
GOOGLE = "google"
UNKNOWN = "unknown"

# TTL labels. The Anthropic cache-write rate depends on which one was used, and
# the response reports the split (usage.cache_creation.ephemeral_{5m,1h}_input_tokens),
# so this is read back from the provider rather than inferred from our config.
TTL_5M = "5m"
TTL_1H = "1h"


@dataclass(frozen=True)
class CacheRates:
    """Cached-token rates as multiples of the model's normal input rate.

    ``cached_inside_prompt_tokens`` is the normalization flag described in the
    module docstring: True means the provider already billed those tokens at
    full rate inside ``prompt_tokens`` and we owe a rebate; False means they are
    reported separately and we owe their cost.

    ``reports_cache`` is False for families whose client gives us no cache
    telemetry at all. It exists so a cache hit rate of 0 can be rendered as
    "not reported" instead of "caching is broken" — the two are
    indistinguishable from the token counts alone.
    """

    read: float
    write_5m: Optional[float]
    write_1h: Optional[float]
    cached_inside_prompt_tokens: bool
    reports_cache: bool

    def write_multiplier(self, ttl: str) -> Optional[float]:
        return self.write_1h if ttl == TTL_1H else self.write_5m


_RATES = {
    # Cache reads 0.1x, writes 1.25x at the 5-minute TTL and 2x at the 1-hour
    # TTL. Reported alongside input_tokens, so each is billed on its own.
    ANTHROPIC: CacheRates(
        read=0.10, write_5m=1.25, write_1h=2.00,
        cached_inside_prompt_tokens=False, reports_cache=True,
    ),
    # Automatic caching, no write concept and no write charge. Cached tokens
    # ride inside prompt_tokens at full rate and are discounted 50%.
    OPENAI: CacheRates(
        read=0.50, write_5m=None, write_1h=None,
        cached_inside_prompt_tokens=True, reports_cache=True,
    ),
    # The Gemini client surfaces no cached-token counts today, so there is
    # nothing to price and nothing to report.
    GOOGLE: CacheRates(
        read=0.0, write_5m=None, write_1h=None,
        cached_inside_prompt_tokens=False, reports_cache=False,
    ),
    UNKNOWN: CacheRates(
        read=0.0, write_5m=None, write_1h=None,
        cached_inside_prompt_tokens=False, reports_cache=False,
    ),
}


# Cache READS are 0.1x the base input rate on every Claude model but two: Claude
# Fable 5.1 and Claude Mythos 5.1 read at 0.025x ($0.25/MTok against a $10 base
# input rate). Cache WRITES are unaffected and stay 1.25x/2x everywhere.
#
# The match is on the VERSION-QUALIFIED tag, deliberately not on the family.
# ``claude-fable-5`` and ``claude-mythos-5`` still read at the standard 0.1x, so
# a bare ``fable`` / ``mythos`` substring -- the style the Anthropic client uses
# for its sampling-parameter tags -- would sweep them in and under-report them
# by 4x: the same error as the one this guards against, pointing the other way.
#
# The trailing ``(?!\d)`` stops a future ``fable-5-10`` from inheriting the
# cheaper rate. An id we do not recognize keeps the standard multiplier rather
# than guessing downward, because under-reporting spend is the worse failure:
# an over-report gets questioned, an under-report gets budgeted against.
_REDUCED_CACHE_READ_RE = re.compile(r"(?:fable|mythos)-5-1(?!\d)")
_REDUCED_CACHE_READ = 0.025


def is_anthropic_model_id(model_id: Optional[str]) -> bool:
    """Whether a deployment name denotes an Anthropic model.

    Deployment names are admin-chosen, so this is a heuristic: Foundry and
    Bedrock default to a name carrying the family (``claude-haiku-4-5``,
    ``anthropic.claude-sonnet-5``), and a renamed deployment still matches as
    long as it keeps the family in the name.
    """
    name = (model_id or "").strip().lower()
    return "claude" in name or "anthropic" in name


def _is_gemini_model_id(model_id: Optional[str]) -> bool:
    name = (model_id or "").strip().lower()
    return "gemini" in name or "bison" in name or "gecko" in name


def resolve_family(provider_type: Optional[str], model_id: Optional[str] = None) -> str:
    """Billing family for a (provider, model) pair.

    The model id decides for every multi-vendor surface — Bedrock, Vertex,
    Azure and OpenAI-compatible gateways all serve more than one family, and a
    gateway alias fronting Claude prices as Claude no matter which wire protocol
    carried it.
    """
    ptype = (provider_type or "").strip().lower()
    if ptype == "anthropic":
        return ANTHROPIC
    if ptype == "google":
        return GOOGLE
    if is_anthropic_model_id(model_id):
        return ANTHROPIC
    if _is_gemini_model_id(model_id):
        return GOOGLE
    if ptype in ("openai", "azure", "custom", "bedrock", "vertex"):
        # Bedrock and Vertex also serve first-party model families (Nova,
        # Gemini) that we have no cache telemetry for; those fall through to
        # the OpenAI-shaped rebate only when the client actually reports a
        # cached-token count, which today it does not.
        return OPENAI if ptype in ("openai", "azure", "custom") else UNKNOWN
    return UNKNOWN


def rates_for(provider_type: Optional[str], model_id: Optional[str] = None) -> CacheRates:
    """Cache rates for a (provider, model) pair.

    The family fixes the shape of the arithmetic; the model id can still move a
    single rate within it, which is why this is resolved per call rather than
    looked up once per family.
    """
    family = resolve_family(provider_type, model_id)
    base = _RATES[family]
    if family == ANTHROPIC and _REDUCED_CACHE_READ_RE.search((model_id or "").lower()):
        return replace(base, read=_REDUCED_CACHE_READ)
    return base


def cached_input_cost(
    rate_per_million: float,
    prompt_tokens: int,
    cache_read_tokens: int,
    cache_write_5m_tokens: int,
    cache_write_1h_tokens: int,
    provider_type: Optional[str],
    model_id: Optional[str] = None,
) -> float:
    """Input-token cost in USD, cached tokens priced by family and TTL.

    ``prompt_tokens`` is whatever the provider called uncached input. For an
    OpenAI-family response that number already includes the cached tokens, so
    the read rate is applied as a rebate; for an Anthropic-family response it
    does not, so the read and write rates are additive.
    """
    if not rate_per_million or rate_per_million <= 0:
        return 0.0
    rates = rates_for(provider_type, model_id)
    per_token = float(rate_per_million) / 1_000_000

    cost = max(int(prompt_tokens or 0), 0) * per_token

    reads = max(int(cache_read_tokens or 0), 0)
    if reads:
        if rates.cached_inside_prompt_tokens:
            # Already charged at full rate inside prompt_tokens; refund the
            # difference between full price and the cached price.
            cost -= reads * per_token * (1.0 - rates.read)
        else:
            cost += reads * per_token * rates.read

    for tokens, ttl in ((cache_write_5m_tokens, TTL_5M), (cache_write_1h_tokens, TTL_1H)):
        n = max(int(tokens or 0), 0)
        if not n:
            continue
        multiplier = rates.write_multiplier(ttl)
        if multiplier:
            cost += n * per_token * multiplier

    return max(cost, 0.0)


def cache_hit_rate(
    prompt_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
    provider_type: Optional[str],
    model_id: Optional[str] = None,
) -> Optional[float]:
    """Share of input tokens served from cache, or None when unmeasurable.

    None means the family reports no cache telemetry — rendering that as 0%
    would be indistinguishable from caching being broken, which is the failure
    mode this return type exists to prevent.

    The denominator differs by family for the same reason the cost does: on an
    Anthropic-family response the cached tokens sit outside ``prompt_tokens``
    and have to be added back in; on an OpenAI-family response they are already
    inside it.
    """
    rates = rates_for(provider_type, model_id)
    if not rates.reports_cache:
        return None

    prompt = max(int(prompt_tokens or 0), 0)
    reads = max(int(cache_read_tokens or 0), 0)
    writes = max(int(cache_creation_tokens or 0), 0)

    total = prompt if rates.cached_inside_prompt_tokens else prompt + reads + writes
    if total <= 0:
        return None
    return min(reads / total, 1.0)
