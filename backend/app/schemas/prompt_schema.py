from pydantic import BaseModel
from typing import Optional, List, Any
from datetime import datetime


class PromptParameter(BaseModel):
    name: str                          # placeholder key: {{name}}
    label: Optional[str] = None
    type: str = 'text'                 # 'text' | 'number' | 'enum' | 'date' | 'date_range'
    required: bool = False
    default: Optional[Any] = None
    options: Optional[List[str]] = None  # for type == 'enum'


class PromptCreate(BaseModel):
    title: Optional[str] = None
    text: str
    mode: str = 'chat'
    model_id: Optional[str] = None
    mentions: Optional[List[dict]] = None
    parameters: Optional[List[PromptParameter]] = None
    scope: str = 'agent'               # 'agent' | 'global' | 'private'
    is_starter: bool = False
    data_source_ids: List[str] = []


class PromptUpdate(BaseModel):
    title: Optional[str] = None
    text: Optional[str] = None
    mode: Optional[str] = None
    model_id: Optional[str] = None
    mentions: Optional[List[dict]] = None
    parameters: Optional[List[PromptParameter]] = None
    scope: Optional[str] = None
    is_starter: Optional[bool] = None
    data_source_ids: Optional[List[str]] = None


class PromptResponse(BaseModel):
    id: str
    title: Optional[str] = None
    text: str
    mode: str
    model_id: Optional[str] = None
    mentions: Optional[List[dict]] = None
    parameters: Optional[List[PromptParameter]] = None
    scope: str
    is_starter: bool
    data_source_ids: List[str] = []
    user_id: Optional[str] = None
    created_at: Optional[datetime] = None
    can_manage: bool = False           # caller can edit this prompt

    class Config:
        from_attributes = True


class PromptListResponse(BaseModel):
    prompts: List[PromptResponse]
    meta: dict


# ── run / run-for ──

class PromptRunRequest(BaseModel):
    parameters: Optional[dict] = None


class PromptRunResponse(BaseModel):
    report_id: str


class PromptRunForRequest(BaseModel):
    principal_type: str                       # 'users' | 'group'
    user_ids: Optional[List[str]] = None      # when principal_type == 'users'
    group_id: Optional[str] = None            # when principal_type == 'group'
    parameters: Optional[dict] = None
    # Extra places to deliver each target's result, on top of the report +
    # in-app inbox they always get. Currently 'teams' (the target's Teams DM)
    # and 'email' (the target's account email). Unknown values are ignored.
    delivery_channels: Optional[List[str]] = None


class PromptRunForResponse(BaseModel):
    ran: int
    skipped: int
    skipped_user_ids: List[str] = []


class RunForTargetUser(BaseModel):
    id: str
    name: Optional[str] = None
    email: Optional[str] = None


class RunForTargetGroup(BaseModel):
    id: str
    name: str
    member_count: int
    eligible_count: int


class PromptRunForTargetsResponse(BaseModel):
    users: List[RunForTargetUser] = []
    groups: List[RunForTargetGroup] = []
