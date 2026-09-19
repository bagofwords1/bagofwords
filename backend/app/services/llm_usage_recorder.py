from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm_model import LLMModel
from app.models.llm_usage_record import LLMUsageRecord

# Anthropic cache-read price as a multiple of the base input rate. 0.1x is the
# standard, but Claude Fable 5.1 and Claude Mythos 5.1 bill a cache hit at
# 0.025x — $0.25/MTok against their $10 base. Charging those two at the
# standard rate overstates every cached read on them by 4x, and cached reads
# dominate a long agent run, so the console's cost figure would drift high on
# exactly the most expensive models we offer.
#
# Matched on the version-qualified tag, not the family: Claude Fable 5 and
# Claude Mythos 5 are still 0.1x ($1/MTok), so a bare "fable-5" or "mythos"
# would sweep them in and under-report their cost by 4x — the same bug
# pointing the other way.
# https://platform.claude.com/docs/en/about-claude/pricing (Model pricing;
# "Cache hits and refreshes on Claude Fable 5.1 and Claude Mythos 5.1 are
# priced at 0.025x the base input price. All other models use the standard
# 0.1x multiplier.")
_ANTHROPIC_CACHE_READ_MULTIPLIER = 0.1
_ANTHROPIC_REDUCED_CACHE_READ_MULTIPLIER = 0.025
_ANTHROPIC_REDUCED_CACHE_READ_TAGS = ("fable-5-1", "mythos-5-1")


def _anthropic_cache_read_multiplier(model_id: str | None) -> float:
    """Cache-read multiplier for an Anthropic model id, relative to input rate."""
    mid = (model_id or "").lower()
    if any(tag in mid for tag in _ANTHROPIC_REDUCED_CACHE_READ_TAGS):
        return _ANTHROPIC_REDUCED_CACHE_READ_MULTIPLIER
    return _ANTHROPIC_CACHE_READ_MULTIPLIER


class LLMUsageRecorderService:
    """Persist per-call LLM token/cost usage."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def record(
        self,
        *,
        scope: str,
        scope_ref_id: str | None,
        llm_model: LLMModel,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        organization_id: str | None = None,
        user_id: str | None = None,
        report_id: str | None = None,
        data_source_id: str | None = None,
        agent_execution_id: str | None = None,
        routed: bool = False,
        baseline_model_id: str | None = None,
    ) -> LLMUsageRecord:

        provider_type = llm_model.provider.provider_type if llm_model.provider else ""
        input_cost = self._calc_input_cost(
            llm_model, prompt_tokens, cache_read_tokens, cache_creation_tokens, provider_type
        )
        output_cost = self._calc_output_cost(llm_model, completion_tokens)

        # Org is always knowable from the model itself; fall back to it when the
        # caller didn't supply explicit attribution. The other dimensions stay
        # NULL when unknown (e.g. background jobs not tied to a user/report).
        org_id = organization_id or (
            str(llm_model.organization_id) if getattr(llm_model, "organization_id", None) else None
        )

        record = LLMUsageRecord(
            scope=scope,
            scope_ref_id=scope_ref_id,
            organization_id=org_id,
            user_id=user_id,
            report_id=report_id,
            data_source_id=data_source_id,
            agent_execution_id=agent_execution_id,
            llm_model_id=str(llm_model.id),
            model_id=llm_model.model_id,
            provider_type=provider_type,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            input_cost_usd=input_cost,
            output_cost_usd=output_cost,
            total_cost_usd=input_cost + output_cost,
            routed=bool(routed),
            baseline_model_id=baseline_model_id,
        )
        self.db.add(record)
        await self.db.flush()

        return record

    @staticmethod
    def _calc_input_cost(
        llm_model: LLMModel,
        tokens: int,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        provider_type: str = "",
    ) -> float:
        rate = llm_model.get_input_cost_rate()
        if rate is None:
            return 0.0
        rate_f = float(rate)
        # Non-cached input tokens at full rate (Anthropic excludes cached tokens
        # from input_tokens; OpenAI includes them, so we handle both below).
        cost = (tokens / 1_000_000) * rate_f if tokens else 0.0
        if provider_type == "anthropic":
            # Cache reads: 0.1× input rate on most models, 0.025× on Fable 5.1
            # and Mythos 5.1 (see _anthropic_cache_read_multiplier).
            # Cache writes: billed at 1.25× input rate.
            if cache_read_tokens:
                multiplier = _anthropic_cache_read_multiplier(llm_model.model_id)
                cost += (cache_read_tokens / 1_000_000) * rate_f * multiplier
            if cache_creation_tokens:
                cost += (cache_creation_tokens / 1_000_000) * rate_f * 1.25
        elif provider_type in ("openai", "azure"):
            # OpenAI/Azure include cached tokens in prompt_tokens at full rate,
            # but actually charge 0.5× for those tokens. Apply the 50% discount.
            if cache_read_tokens:
                cost -= (cache_read_tokens / 1_000_000) * rate_f * 0.5
        return max(cost, 0.0)

    @staticmethod
    def _calc_output_cost(llm_model: LLMModel, tokens: int) -> float:
        rate = llm_model.get_output_cost_rate()
        if not tokens or rate is None:
            return 0.0
        return (tokens / 1_000_000) * float(rate)

