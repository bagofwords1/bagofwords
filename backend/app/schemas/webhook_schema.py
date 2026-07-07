from datetime import datetime
from typing import List, Optional, Literal

from pydantic import BaseModel, ConfigDict, Field


WebhookSource = Literal["github", "jira", "generic"]
AuthMode = Literal["hmac", "token", "url_token"]
TriggerMode = Literal["chat", "deep"]


class WebhookCreate(BaseModel):
    """Report-bound webhook creation (legacy scope)."""
    name: str = "Webhook"
    source: WebhookSource = "generic"
    auth_mode: AuthMode = "token"
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


class TriggerCreate(BaseModel):
    """Standalone trigger (spawn mode): user-owned, carries its own run spec."""
    name: str = "Trigger"
    source: WebhookSource = "generic"
    auth_mode: AuthMode = "token"
    auth_header_name: Optional[str] = "Authorization"
    classify_enabled: bool = False
    classifier_prompt: Optional[str] = None
    # Run spec (mirrors Prompt's execution shape)
    task_template: Optional[str] = None
    mode: TriggerMode = "chat"
    model_id: Optional[str] = None
    data_source_ids: List[str] = Field(default_factory=list)


class TriggerUpdate(BaseModel):
    name: Optional[str] = None
    source: Optional[WebhookSource] = None
    auth_mode: Optional[AuthMode] = None
    auth_header_name: Optional[str] = None
    classify_enabled: Optional[bool] = None
    classifier_prompt: Optional[str] = None
    is_active: Optional[bool] = None
    task_template: Optional[str] = None
    mode: Optional[TriggerMode] = None
    model_id: Optional[str] = None
    data_source_ids: Optional[List[str]] = None


class WebhookDataSourceInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    type: Optional[str] = None


class WebhookSchema(BaseModel):
    """Public representation — never includes the raw signing secret."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    report_id: Optional[str] = None  # NULL for standalone triggers
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

    # Trigger run spec (spawn mode)
    task_template: Optional[str] = None
    mode: TriggerMode = "chat"
    model_id: Optional[str] = None
    data_sources: List[WebhookDataSourceInfo] = []

    # Computed/derived fields filled by the service
    delivery_url: Optional[str] = None
    # Full secret — only present in the create / rotate responses, shown once.
    secret: Optional[str] = None
    # Number of sessions this trigger has spawned (list view).
    run_count: int = 0


class TriggerRunSchema(BaseModel):
    """One spawned session in a trigger's run history."""
    report_id: str
    title: str
    created_at: Optional[datetime] = None
    status: Optional[str] = None  # latest system-completion status in the report
    event_summary: Optional[str] = None


class TriggerRunListResponse(BaseModel):
    runs: List[TriggerRunSchema]
    total: int
