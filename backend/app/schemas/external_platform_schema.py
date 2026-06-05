from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from datetime import datetime
from enum import Enum

class PlatformType(str, Enum):
    SLACK = "slack"
    TEAMS = "teams"
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    MCP = "mcp"
    EXCEL = "excel"

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
    auto_link_by_email: bool = True

class TeamsConfig(BaseModel):
    app_id: str
    client_secret: str
    tenant_id: str
    webhook_url: Optional[str] = None
    auto_link_by_email: bool = True

class WhatsAppConfig(BaseModel):
    access_token: str
    phone_number_id: str
    waba_id: str
    app_secret: str
    verify_token: str
    webhook_url: Optional[str] = None

class EmailConfig(BaseModel):
    """Email integration config.

    SMTP fields are required (outbound transport). IMAP fields are optional —
    when provided, the integration also becomes a conversational channel (the
    analyst can be emailed). One integration, capability derived from fields.
    """

    # --- Outbound (required) ---
    smtp_host: str
    smtp_port: int = 587
    smtp_username: str
    smtp_password: str
    smtp_security: str = "starttls"  # "starttls" | "ssl" | "none"
    from_address: Optional[str] = None  # defaults to smtp_username
    from_name: Optional[str] = "Bag of words Analyst"

    # --- Inbound (optional -> turns it into a channel) ---
    imap_host: Optional[str] = None
    imap_port: int = 993
    imap_username: Optional[str] = None
    imap_password: Optional[str] = None
    imap_use_ssl: bool = True
    imap_mailbox: str = "INBOX"

    # --- Channel behavior / security ---
    allowed_domains: List[str] = Field(default_factory=list)
    auto_link_by_email: bool = True
    require_auth_pass: bool = True

    # "password" | "xoauth2" (v1 ships password)
    auth_type: str = "password"
    webhook_endpoint: Optional[str] = None