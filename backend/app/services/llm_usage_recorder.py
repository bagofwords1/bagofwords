from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import pricing
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
        cache_write_5m_tokens: int = 0,
        cache_write_1h_tokens: int = 0,
        reasoning_tokens: int = 0,
        organization_id: str | None = None,
        user_id: str | None = None,
        report_id: str | None = None,
        data_source_id: str | None = None,
        agent_execution_id: str | None = None,
        routed: bool = False,
        baseline_model_id: str | None = None,
    ) -> LLMUsageRecord:

        provider_type = llm_model.provider.provider_type if llm_model.provider else ""
        # A write with no TTL breakdown predates the split (or came from a
        # client that does not report it): bill it at the 5-minute rate, which
        # is the API default and never over-charges.
        if cache_creation_tokens and not (cache_write_5m_tokens or cache_write_1h_tokens):
            cache_write_5m_tokens = cache_creation_tokens
        input_cost = self._calc_input_cost(
            llm_model, prompt_tokens, cache_read_tokens, cache_creation_tokens, provider_type,
            cache_write_5m_tokens=cache_write_5m_tokens,
            cache_write_1h_tokens=cache_write_1h_tokens,
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
            cache_write_1h_tokens=cache_write_1h_tokens,
            reasoning_tokens=reasoning_tokens,
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
        cache_write_5m_tokens: int = 0,
        cache_write_1h_tokens: int = 0,
    ) -> float:
        """Input cost, with cached tokens priced by MODEL FAMILY and TTL.

        Delegates to app.ai.llm.pricing, which keys on the family rather than
        the provider account: Claude prices identically whether it is reached
        first-party, through Vertex, Azure Foundry, Bedrock or an
        OpenAI-compatible gateway, and pricing on provider_type alone silently
        charged $0 for cached tokens on some of those routes and applied an
        OpenAI rebate on others.

        cache_creation_tokens is accepted for backwards compatibility; when the
        per-TTL split is absent the whole write bills at the 5-minute rate.
        """
        rate = llm_model.get_input_cost_rate()
        if rate is None:
            return 0.0
        if cache_creation_tokens and not (cache_write_5m_tokens or cache_write_1h_tokens):
            cache_write_5m_tokens = cache_creation_tokens
        return pricing.cached_input_cost(
            rate_per_million=float(rate),
            prompt_tokens=tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_5m_tokens=cache_write_5m_tokens,
            cache_write_1h_tokens=cache_write_1h_tokens,
            provider_type=provider_type,
            model_id=getattr(llm_model, "model_id", None),
        )

    @staticmethod
    def _calc_output_cost(llm_model: LLMModel, tokens: int) -> float:
        rate = llm_model.get_output_cost_rate()
        if not tokens or rate is None:
            return 0.0
        return (tokens / 1_000_000) * float(rate)

