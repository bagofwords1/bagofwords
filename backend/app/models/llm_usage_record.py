from sqlalchemy import Column, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema


class LLMUsageRecord(BaseSchema):
    __tablename__ = "llm_usage_records"

    scope = Column(String, nullable=False)
    scope_ref_id = Column(String, nullable=True)

    llm_model_id = Column(String, ForeignKey("llm_models.id"), nullable=False)
    llm_model = relationship("LLMModel", lazy="selectin")

    model_id = Column(String, nullable=False)
    provider_type = Column(String, nullable=False)

    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)

    input_cost_usd = Column(Numeric(18, 6), nullable=False, default=0)
    output_cost_usd = Column(Numeric(18, 6), nullable=False, default=0)
    total_cost_usd = Column(Numeric(18, 6), nullable=False, default=0)

