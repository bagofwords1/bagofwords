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
    instructions_coverage: str  # Rename from instructions_efficiency
    instructions_effectiveness: float  # New field for judge metrics
    context_effectiveness: float  # New field for judge metrics
    response_quality: float  # New field for judge metrics
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
    instructions_coverage: List[TimeSeriesPointFloat]  # Rename from instructions_efficiency
    instructions_effectiveness: List[TimeSeriesPointFloat]  # New judge metric
    context_effectiveness: List[TimeSeriesPointFloat]  # New judge metric
    response_quality: List[TimeSeriesPointFloat]  # New judge metric
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

# Diagnosis Schemas
class DiagnosisStepData(BaseModel):
    step_id: str
    step_title: str
    step_status: str
    step_code: Optional[str] = None
    step_data_model: Optional[Dict] = None
    created_at: datetime

class DiagnosisFeedbackData(BaseModel):
    feedback_id: str
    direction: int
    message: Optional[str] = None
    created_at: datetime

class DiagnosisItemData(BaseModel):
    id: str
    head_completion_id: str
    head_completion_prompt: str
    problematic_completion_id: str
    problematic_completion_content: Optional[str] = None
    user_id: str
    user_name: str
    user_email: Optional[str] = None
    report_id: str
    issue_type: str  # "failed_step", "negative_feedback", or "both"
    step_info: Optional[DiagnosisStepData] = None
    feedback_info: Optional[DiagnosisFeedbackData] = None
    created_at: datetime
    trace_url: Optional[str] = None

class DiagnosisMetrics(BaseModel):
    diagnosis_items: List[DiagnosisItemData]
    total_items: int
    total_queries_count: int
    failed_steps_count: int
    negative_feedback_count: int
    code_errors_count: int
    validation_errors_count: int
    date_range: DateRange

# Trace Schemas
class TraceCompletionData(BaseModel):
    completion_id: str
    role: str
    content: Optional[str] = None
    reasoning: Optional[str] = None
    created_at: datetime
    status: Optional[str] = None
    has_issue: bool = False
    issue_type: Optional[str] = None
    instructions_effectiveness: Optional[int] = None
    context_effectiveness: Optional[int] = None
    response_score: Optional[int] = None

class TraceStepData(BaseModel):
    step_id: str
    title: str
    status: str
    code: Optional[str] = None
    data_model: Optional[Dict] = None
    data: Optional[Dict] = None
    created_at: datetime
    completion_id: str
    has_issue: bool = False

class TraceFeedbackData(BaseModel):
    feedback_id: str
    direction: int
    message: Optional[str] = None
    created_at: datetime
    completion_id: str

class TraceData(BaseModel):
    report_id: str
    head_completion: TraceCompletionData
    completions: List[TraceCompletionData]
    steps: List[TraceStepData]
    feedbacks: List[TraceFeedbackData]
    issue_completion_id: str
    issue_type: str
    user_name: str
    user_email: Optional[str] = None