from pydantic import BaseModel
from app.schemas.base import UTCDatetime


class TablePrompt(BaseModel):
    execution_id: str
    prompt: str
    used_at: UTCDatetime
    success: bool


class TablePromptsResponse(BaseModel):
    items: list[TablePrompt]
    next_offset: int | None
