from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class ApiKeyCreate(BaseModel):
    name: str = "Default"
    expires_at: Optional[datetime] = None


class ApiKeyResponse(BaseModel):
    id: str
    name: str
    key_prefix: str
    created_at: datetime
    expires_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ApiKeyCreated(ApiKeyResponse):
    """Response when creating a new API key - includes the full key (shown only once)"""
    key: str










