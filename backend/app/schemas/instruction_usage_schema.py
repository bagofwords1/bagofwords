from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


class InstructionUsageEventCreate(BaseModel):
    org_id: str
    report_id: Optional[str] = None
    instruction_id: str
    user_id: Optional[str] = None
    load_mode: str                                    # 'always' | 'intelligent'
    load_reason: Optional[str] = None                 # 'always' | 'search_match' | 'mentioned'
    search_score: Optional[float] = None
    search_query_keywords: Optional[List[str]] = None
    source_type: Optional[str] = None                 # 'user' | 'git' | 'ai'
    category: Optional[str] = None
    title: Optional[str] = None
    user_role: Optional[str] = None
    role_weight: Optional[float] = None


class InstructionUsageEventSchema(InstructionUsageEventCreate):
    id: str
    used_at: datetime
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class InstructionFeedbackEventCreate(BaseModel):
    org_id: str
    report_id: Optional[str] = None
    instruction_id: str
    completion_feedback_id: str
    feedback_type: str  # 'positive' | 'negative'


class InstructionFeedbackEventSchema(InstructionFeedbackEventCreate):
    id: str
    created_at_event: datetime

    class Config:
        from_attributes = True


class InstructionStatsUpsert(BaseModel):
    org_id: str
    report_id: Optional[str] = None
    instruction_id: str
    usage_count_delta: int = 0
    always_count_delta: int = 0
    intelligent_count_delta: int = 0
    mentioned_count_delta: int = 0
    weighted_usage_delta: float = 0.0
    pos_feedback_delta: int = 0
    neg_feedback_delta: int = 0
    weighted_pos_delta: float = 0.0
    weighted_neg_delta: float = 0.0
    unique_user_delta: int = 0
    last_used_at: Optional[datetime] = None
    last_feedback_at: Optional[datetime] = None


class InstructionStatsSchema(BaseModel):
    id: str
    org_id: str
    report_id: Optional[str]
    instruction_id: str
    usage_count: int
    always_count: int
    intelligent_count: int
    mentioned_count: int
    weighted_usage_count: float
    pos_feedback_count: int
    neg_feedback_count: int
    weighted_pos_feedback: float
    weighted_neg_feedback: float
    unique_users: int
    last_used_at: Optional[datetime]
    last_feedback_at: Optional[datetime]
    updated_at_stats: datetime

    class Config:
        from_attributes = True
