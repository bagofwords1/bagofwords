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
    ) -> LLMUsageRecord:

        input_cost = self._calc_input_cost(llm_model, prompt_tokens)
        output_cost = self._calc_output_cost(llm_model, completion_tokens)

        record = LLMUsageRecord(
            scope=scope,
            scope_ref_id=scope_ref_id,
            llm_model_id=str(llm_model.id),
            model_id=llm_model.model_id,
            provider_type=llm_model.provider.provider_type if llm_model.provider else "",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            input_cost_usd=input_cost,
            output_cost_usd=output_cost,
            total_cost_usd=input_cost + output_cost,
        )
        self.db.add(record)
        await self.db.flush()

        return record

    @staticmethod
    def _calc_input_cost(llm_model: LLMModel, tokens: int) -> float:
        rate = llm_model.get_input_cost_rate()
        if not tokens or rate is None:
            return 0.0
        return (tokens / 1_000_000) * float(rate)

    @staticmethod
    def _calc_output_cost(llm_model: LLMModel, tokens: int) -> float:
        rate = llm_model.get_output_cost_rate()
        if not tokens or rate is None:
            return 0.0
        return (tokens / 1_000_000) * float(rate)

