from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from datetime import datetime


class UserDataSourceCredentialsCreate(BaseModel):
    auth_mode: str = Field(..., description="Registry auth key, e.g. userpass, pat, iam")
    credentials: Dict[str, Any] = Field(..., description="Credential fields per auth_mode")
    is_primary: Optional[bool] = Field(default=True)
    expires_at: Optional[datetime] = None
    metadata_json: Optional[Dict[str, Any]] = None


class UserDataSourceCredentialsUpdate(BaseModel):
    # If changing auth_mode, credentials must also be provided at runtime validation
    auth_mode: Optional[str] = None
    credentials: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None
    is_primary: Optional[bool] = None
    expires_at: Optional[datetime] = None
    metadata_json: Optional[Dict[str, Any]] = None


class UserDataSourceCredentialsSchema(BaseModel):
    id: str
    data_source_id: str
    user_id: str
    organization_id: str
    auth_mode: str
    is_active: bool
    is_primary: bool
    last_used_at: Optional[datetime]
    expires_at: Optional[datetime]
    metadata_json: Optional[Dict[str, Any]]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True