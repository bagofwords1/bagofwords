from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from app.schemas.external_platform_schema import PlatformType

class ExternalUserMappingBase(BaseModel):
    platform_type: PlatformType
    external_user_id: str
    external_email: Optional[str] = None
    external_name: Optional[str] = None
    app_user_id: Optional[str] = None  # Allow null initially
    is_verified: bool = False

class ExternalUserMappingCreate(ExternalUserMappingBase):
    pass

class ExternalUserMappingUpdate(BaseModel):
    external_email: Optional[str] = None
    external_name: Optional[str] = None
    is_verified: Optional[bool] = None

class ExternalUserMappingSchema(ExternalUserMappingBase):
    id: str
    organization_id: str
    platform_id: str  
    verification_token: Optional[str] = None
    verification_expires_at: Optional[datetime] = None
    last_verified_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True