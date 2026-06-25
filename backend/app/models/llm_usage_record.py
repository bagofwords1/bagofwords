from sqlalchemy import Column, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema


class LLMUsageRecord(BaseSchema):
    __tablename__ = "llm_usage_records"

    scope = Column(String, nullable=False)
    scope_ref_id = Column(String, nullable=True)

    # Attribution columns (all nullable, populated best-effort at record time).
    # organization_id is set from the model's org on every record; user_id /
    # report_id / data_source_id come from the agent run context (see the
    # usage attribution contextvar in app.ai.llm.usage_attribution). They let
    # the Cost console break LLM spend down by user / agent (data source) /
    # group over time. Older rows predate these columns and stay NULL, which
    # the cost queries surface as an "unattributed" bucket.
    organization_id = Column(String, ForeignKey("organizations.id"), nullable=True, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=True, index=True)
    report_id = Column(String, ForeignKey("reports.id"), nullable=True, index=True)
    data_source_id = Column(String, ForeignKey("data_sources.id"), nullable=True, index=True)

    llm_model_id = Column(String, ForeignKey("llm_models.id"), nullable=False)
    llm_model = relationship("LLMModel", lazy="selectin")

    model_id = Column(String, nullable=False)
    provider_type = Column(String, nullable=False)

    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    cache_read_tokens = Column(Integer, nullable=False, default=0)
    cache_creation_tokens = Column(Integer, nullable=False, default=0)

    input_cost_usd = Column(Numeric(18, 6), nullable=False, default=0)
    output_cost_usd = Column(Numeric(18, 6), nullable=False, default=0)
    total_cost_usd = Column(Numeric(18, 6), nullable=False, default=0)

