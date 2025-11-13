from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


class TestMetricsSchema(BaseModel):
    total_test_cases: int
    total_test_runs: int
    last_result_status: Optional[str] = None  # pass|fail|error|in_progress|success|stopped
    last_result_at: Optional[datetime] = None


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


