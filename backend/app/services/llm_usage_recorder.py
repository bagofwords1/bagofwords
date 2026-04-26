from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm_model import LLMModel
from app.models.llm_usage_record import LLMUsageRecord


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
    ) -> LLMUsageRecord:

        provider_type = llm_model.provider.provider_type if llm_model.provider else ""
        input_cost = self._calc_input_cost(
            llm_model, prompt_tokens, cache_read_tokens, cache_creation_tokens, provider_type
        )
        output_cost = self._calc_output_cost(llm_model, completion_tokens)

        record = LLMUsageRecord(
            scope=scope,
            scope_ref_id=scope_ref_id,
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
            # Cache reads: billed at 0.1× input rate.
            # Cache writes: billed at 1.25× input rate.
            if cache_read_tokens:
                cost += (cache_read_tokens / 1_000_000) * rate_f * 0.1
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

