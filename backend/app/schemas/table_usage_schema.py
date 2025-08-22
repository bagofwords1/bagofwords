from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime


class TableUsageEventCreate(BaseModel):
    org_id: str
    report_id: Optional[str] = None
    step_id: str
    user_id: Optional[str] = None
    table_fqn: str
    datasource_table_id: Optional[str] = None
    source_type: str
    columns: Optional[List[str]] = None
    success: bool = True
    user_role: Optional[str] = None
    role_weight: Optional[float] = None
    # intentionally no code/query hashes for now


class TableUsageEventSchema(TableUsageEventCreate):
    id: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class TableFeedbackEventCreate(BaseModel):
    org_id: str
    report_id: Optional[str] = None
    step_id: Optional[str] = None
    completion_feedback_id: str
    table_fqn: str
    datasource_table_id: Optional[str] = None
    feedback_type: str  # 'positive' | 'negative'


class TableFeedbackEventSchema(TableFeedbackEventCreate):
    id: str
    created_at_event: datetime

    class Config:
        from_attributes = True


class TableStatsUpsert(BaseModel):
    org_id: str
    report_id: Optional[str] = None
    table_fqn: str
    datasource_table_id: Optional[str] = None
    usage_count_delta: int = 0
    success_count_delta: int = 0
    weighted_usage_delta: float = 0.0
    pos_feedback_delta: int = 0
    neg_feedback_delta: int = 0
    weighted_pos_delta: float = 0.0
    weighted_neg_delta: float = 0.0
    unique_user_delta: int = 0
    admin_usage_delta: int = 0
    failure_delta: int = 0
    last_used_at: Optional[datetime] = None
    last_feedback_at: Optional[datetime] = None


class TableStatsSchema(BaseModel):
    id: str
    org_id: str
    report_id: Optional[str]
    table_fqn: str
    datasource_table_id: Optional[str]
    usage_count: int
    success_count: int
    weighted_usage_count: float
    pos_feedback_count: int
    neg_feedback_count: int
    weighted_pos_feedback: float
    weighted_neg_feedback: float
    unique_users: int
    trusted_usage_count: int
    failure_count: int
    last_used_at: Optional[datetime]
    last_feedback_at: Optional[datetime]
    updated_at_stats: datetime

    class Config:
        from_attributes = True

