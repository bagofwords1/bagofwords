from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from datetime import datetime
from enum import Enum

class PlatformType(str, Enum):
    SLACK = "slack"
    TEAMS = "teams"
    EMAIL = "email"
    MCP = "mcp"

class ExternalPlatformBase(BaseModel):
    platform_type: PlatformType
    platform_config: Dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True

class ExternalPlatformCreate(ExternalPlatformBase):
    pass

class ExternalPlatformUpdate(BaseModel):
    platform_config: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None

class ExternalPlatformSchema(ExternalPlatformBase):
    id: str
    organization_id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

class SlackConfig(BaseModel):
    bot_token: str
    signing_secret: str
    webhook_url: Optional[str] = None

class TeamsConfig(BaseModel):
    app_id: str
    client_secret: str
    webhook_url: Optional[str] = None

class EmailConfig(BaseModel):
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    webhook_endpoint: Optional[str] = None