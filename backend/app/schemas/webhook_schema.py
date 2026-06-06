from datetime import datetime
from typing import Optional, Literal

from pydantic import BaseModel, ConfigDict


WebhookSource = Literal["github", "jira", "generic"]
AuthMode = Literal["hmac", "token", "url_token"]


class WebhookCreate(BaseModel):
    name: str = "Webhook"
    source: WebhookSource = "generic"
    auth_mode: AuthMode = "hmac"
    auth_header_name: Optional[str] = "Authorization"
    classify_enabled: bool = True
    classifier_prompt: Optional[str] = None


class WebhookUpdate(BaseModel):
    name: Optional[str] = None
    source: Optional[WebhookSource] = None
    auth_mode: Optional[AuthMode] = None
    auth_header_name: Optional[str] = None
    classify_enabled: Optional[bool] = None
    classifier_prompt: Optional[str] = None
    is_active: Optional[bool] = None


class WebhookSchema(BaseModel):
    """Public representation — never includes the raw signing secret."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    report_id: str
    name: str
    token: str
    source: WebhookSource
    auth_mode: AuthMode
    auth_header_name: Optional[str] = None
    classify_enabled: bool
    classifier_prompt: Optional[str] = None
    is_active: bool
    last_delivery_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

    # Computed/derived fields filled by the service
    delivery_url: Optional[str] = None
    # Full secret — only present in the create / rotate responses, shown once.
    secret: Optional[str] = None
