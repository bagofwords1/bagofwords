from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class LLMUsageBaseSchema(BaseModel):
    scope: str
    scope_ref_id: Optional[str]
    llm_model_id: str
    model_id: str
    provider_type: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    input_cost_usd: Decimal = Decimal("0")
    output_cost_usd: Decimal = Decimal("0")
    total_cost_usd: Decimal = Decimal("0")


class LLMUsageCreateSchema(LLMUsageBaseSchema):
    pass


class LLMUsageSchema(LLMUsageBaseSchema):
    id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True

