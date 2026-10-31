from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from app.schemas.base import OptionalNaiveUTCInput, OptionalUTCDatetime


class ApiKeyCreate(BaseModel):
    name: str = Field(default="Default", max_length=255)
    expires_at: OptionalNaiveUTCInput = None


class ApiKeyResponse(BaseModel):
    id: str
    name: str
    key_prefix: str
    created_at: datetime
    expires_at: OptionalUTCDatetime = None
    last_used_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ApiKeyCreated(ApiKeyResponse):
    """Response when creating a new API key - includes the full key (shown only once)"""
    key: str










