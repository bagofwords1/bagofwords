from pydantic import BaseModel, Field
from typing import Optional, Dict, List
from datetime import datetime

class MetricsQueryParams(BaseModel):
    start_date: Optional[datetime] = Field(None, description="Start date for metrics query")
    end_date: Optional[datetime] = Field(None, description="End date for metrics query")

class SimpleMetrics(BaseModel):
    total_messages: int
    total_queries: int
    total_feedbacks: int
    accuracy: str
    instructions_efficiency: str
    feedback_efficiency: str
    active_users: int

class MetricsComparison(BaseModel):
    current: SimpleMetrics
    previous: SimpleMetrics
    changes: Dict[str, Dict[str, float]]  # {"metric_name": {"absolute": 10, "percentage": 25.0}}
    period_days: int

# New schemas for time-series data
class TimeSeriesPoint(BaseModel):
    date: str
    value: int

class TimeSeriesPointFloat(BaseModel):
    date: str
    value: float

class DateRange(BaseModel):
    start: str
    end: str

class ActivityMetrics(BaseModel):
    messages: List[TimeSeriesPoint]
    queries: List[TimeSeriesPoint]

class PerformanceMetrics(BaseModel):
    accuracy: List[TimeSeriesPointFloat]
    instructions_efficiency: List[TimeSeriesPointFloat] 
    positive_feedback_rate: List[TimeSeriesPointFloat]

class TimeSeriesMetrics(BaseModel):
    date_range: DateRange
    activity_metrics: ActivityMetrics
    performance_metrics: PerformanceMetrics

class TableUsageData(BaseModel):
    table_name: str
    usage_count: int
    database_name: Optional[str] = None

class TableUsageMetrics(BaseModel):
    top_tables: List[TableUsageData]
    total_queries_analyzed: int
    date_range: DateRange

class TableJoinData(BaseModel):
    table1: str
    table2: str
    join_count: int

class TableJoinsHeatmap(BaseModel):
    table_pairs: List[TableJoinData]
    unique_tables: List[str]
    total_queries_analyzed: int
    date_range: DateRange

class TopUserData(BaseModel):
    user_id: str
    name: str
    email: Optional[str] = None
    role: Optional[str] = None
    messages_count: int
    queries_count: int
    # Remove trend_percentage field

class TopUsersMetrics(BaseModel):
    top_users: List[TopUserData]
    total_users_analyzed: int
    date_range: DateRange

class RecentNegativeFeedbackData(BaseModel):
    id: str
    description: str
    user_name: str
    user_id: str
    completion_id: str
    prompt: Optional[str] = None
    created_at: datetime
    trace: Optional[str] = None  # For diagnosis link

class RecentNegativeFeedbackMetrics(BaseModel):
    recent_feedbacks: List[RecentNegativeFeedbackData]
    total_negative_feedbacks: int
    date_range: DateRange