from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

from app.schemas.api_key_schema import ApiKeyResponse, ApiKeyCreated


class ServiceAccountRoleSummary(BaseModel):
    id: str
    name: str


class ServiceAccountCreate(BaseModel):
    name: str
    description: Optional[str] = None
    # The org role to assign. If omitted, the account gets the system "member"
    # role. Must be a role the creator's own permissions cover.
    role_id: Optional[str] = None


class ServiceAccountUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    role_id: Optional[str] = None
    # Set disabled=True to disable the account (rejects all its keys), False to
    # re-enable it.
    disabled: Optional[bool] = None


class ServiceAccountResponse(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    disabled: bool
    created_at: datetime
    created_by_user_id: Optional[str] = None
    roles: List[ServiceAccountRoleSummary] = []
    key_count: int = 0
    last_used_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ServiceAccountKeyCreate(BaseModel):
    name: str = "Default"
    expires_at: Optional[datetime] = None


class ServiceAccountDetail(ServiceAccountResponse):
    keys: List[ApiKeyResponse] = []
