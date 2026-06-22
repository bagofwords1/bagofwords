from pydantic import BaseModel
from typing import Optional, List, Any
from datetime import datetime


class PromptCatalogBase(BaseModel):
    title: Optional[str] = None
    text: str
    mode: str = 'chat'
    model_id: Optional[str] = None
    mentions: Optional[List[dict]] = None
    scope: str = 'private'              # 'private' | 'agent'
    is_starter: bool = False
    status: str = 'draft'              # 'draft' | 'published'
    default_cron: Optional[str] = None
    default_channel: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    data_source_ids: List[str] = []


class PromptCatalogCreate(PromptCatalogBase):
    pass


class PromptCatalogUpdate(BaseModel):
    title: Optional[str] = None
    text: Optional[str] = None
    mode: Optional[str] = None
    model_id: Optional[str] = None
    mentions: Optional[List[dict]] = None
    scope: Optional[str] = None
    is_starter: Optional[bool] = None
    status: Optional[str] = None
    default_cron: Optional[str] = None
    default_channel: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    data_source_ids: Optional[List[str]] = None


class PromptCatalogResponse(BaseModel):
    id: str
    title: Optional[str] = None
    text: str
    mode: str
    model_id: Optional[str] = None
    mentions: Optional[List[dict]] = None
    scope: str
    is_starter: bool
    status: str
    default_cron: Optional[str] = None
    default_channel: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    data_source_ids: List[str] = []
    user_id: Optional[str] = None
    created_at: Optional[datetime] = None
    # discovery
    subscriber_count: int = 0
    can_assign: bool = False           # does the caller hold assign_prompts on this prompt's agents
    can_manage: bool = False           # does the caller hold manage on this prompt's agents

    class Config:
        from_attributes = True


class PromptListResponse(BaseModel):
    prompts: List[PromptCatalogResponse]
    meta: dict


class SubscribeRequest(BaseModel):
    cron_schedule: str
    channel: Optional[str] = None       # 'teams'|'slack'|'ai_mailbox'|'smtp'|None(=skip)
    run_mode: str = 'append'           # 'append' | 'new_report'


class AssignRequest(BaseModel):
    principal_type: str                 # 'user' | 'group' | 'org'
    principal_id: Optional[str] = None  # required for user/group; ignored for org
    cron_schedule: str
    channel: Optional[str] = None
    run_mode: str = 'append'


class AssignResponse(BaseModel):
    created: int                        # subscriptions created
    skipped: int                        # users skipped (no agent access)
    scheduled_prompt_ids: List[str] = []


class RunNowResponse(BaseModel):
    report_id: str
    completion_id: Optional[str] = None
