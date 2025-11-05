from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


class TestMetricsSchema(BaseModel):
    total_tests: int
    success_rate: float  # 0..1


class TestSuiteSummarySchema(BaseModel):
    id: str
    name: str
    tests_count: int
    last_run_at: Optional[datetime] = None
    last_status: Optional[str] = None  # success|error|in_progress
    pass_rate: Optional[float] = None  # 0..1


class TestDashboardResponseSchema(BaseModel):
    metrics: TestMetricsSchema
    suites: List[TestSuiteSummarySchema]


