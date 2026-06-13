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

    The mailbox model: the analyst owns a mailbox and BOW connects to it
    outbound-only (send via SMTP, receive via IMAP). ``auth_type`` selects how
    BOW authenticates to that mailbox:

    - ``password``  — host/port/username/password (on-prem Exchange, app password)
    - ``microsoft`` — Microsoft 365 app-only OAuth (XOAUTH2); needs an Entra app
      (tenant_id/client_id/client_secret) with IMAP.AccessAsApp + SMTP.SendAsApp
      granted access to the mailbox. Hosts default to Office 365.
    - ``google``    — Google Workspace service account + domain-wide delegation
      (XOAUTH2). Needs the service-account JSON; hosts default to Gmail.

    For OAuth auth types, host/port are optional (provider defaults are applied)
    and passwords are not used.
    """

    # "password" | "microsoft" | "google"
    auth_type: str = "password"

    # --- Outbound / mailbox identity ---
    from_address: Optional[str] = None  # the mailbox; defaults to smtp_username
    from_name: Optional[str] = "Bag of words Analyst"
    smtp_host: Optional[str] = None  # required for password; defaulted for OAuth
    smtp_port: Optional[int] = None
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_security: str = "starttls"  # "starttls" | "ssl" | "none"

    # --- Inbound (optional -> turns it into a channel) ---
    imap_host: Optional[str] = None
    imap_port: Optional[int] = None
    imap_username: Optional[str] = None
    imap_password: Optional[str] = None
    imap_use_ssl: bool = True
    imap_mailbox: str = "INBOX"
    inbound_enabled: bool = False  # explicit toggle; also inferred from imap creds

    # --- Microsoft 365 OAuth (app-only and delegated) ---
    ms_tenant_id: Optional[str] = None
    ms_client_id: Optional[str] = None
    ms_client_secret: Optional[str] = None  # optional for delegated public client
    ms_refresh_token: Optional[str] = None  # delegated (no PowerShell) path

    # --- Google Workspace OAuth ---
    # Service account JSON (DWD), as an object or a JSON string.
    google_service_account_json: Optional[Any] = None
    # Delegated (OAuth client + refresh token) path.
    google_client_id: Optional[str] = None
    google_client_secret: Optional[str] = None
    google_refresh_token: Optional[str] = None

    # --- Channel behavior / security ---
    allowed_domains: List[str] = Field(default_factory=list)
    auto_link_by_email: bool = True
    require_auth_pass: bool = True
    webhook_endpoint: Optional[str] = None