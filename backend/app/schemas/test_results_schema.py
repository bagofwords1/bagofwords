"""
Result JSON schemas for test runs.

Examples
--------
Pass example (rule_results aligned with TestCase.expectations_json.rules):
{
  "totals": { "total": 3, "passed": 3, "failed": 0, "duration_ms": 12450 },
  "rule_results": [
    { "ok": true },
    { "ok": true },
    { "ok": true }
  ]
}

Fail example (with evidence on failing rules):
{
  "totals": { "total": 4, "passed": 2, "failed": 2 },
  "rule_results": [
    { "ok": true },
    {
      "ok": false,
      "message": "Not all required columns are present",
      "actual": ["customer_id","first_name","last_name"],
      "evidence": { "type": "create_data", "occurrence": 1, "step_id": "step_123" }
    },
    {
      "ok": false,
      "message": "clarify called fewer times than required",
      "actual": 0,
      "evidence": { "type": "clarify" }
    },
    { "ok": true }
  ]
}
"""

from typing import Any, Dict, List, Optional, Literal
from pydantic import BaseModel, Field
from datetime import datetime


class RuleEvidence(BaseModel):
    type: Literal["create_data", "clarify", "completion", "judge"]
    occurrence: Optional[int] = None
    step_id: Optional[str] = None


class RuleResult(BaseModel):
    ok: bool
    message: Optional[str] = None
    actual: Optional[Any] = None
    evidence: Optional[RuleEvidence] = None


class TestResultTotals(BaseModel):
    total: int = 0
    passed: int = 0
    failed: int = 0
    duration_ms: Optional[int] = None


class TestResultJsonSchema(BaseModel):
    totals: TestResultTotals
    rule_results: List[RuleResult] = Field(default_factory=list)  # index-aligned with case.rules


# -------- Run status payloads (extracted from test_run_schema) --------
from app.schemas.test_suite_schema import TestRunSchema
from app.schemas.completion_v2_schema import CompletionV2Schema

# Result schema for single TestResult entity (API response)
class TestResultSchema(BaseModel):
    id: str
    run_id: str
    case_id: str
    head_completion_id: str
    report_id: Optional[str] = None
    status: str
    failure_reason: Optional[str] = None
    agent_execution_id: Optional[str] = None
    result_json: Optional[TestResultJsonSchema] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TestRunResultWithCompletions(BaseModel):
    result: 'TestResultSchema'
    report_id: str
    completions: List[CompletionV2Schema] = []

class TestRunStatusResponse(BaseModel):
    run: TestRunSchema
    results: List[TestRunResultWithCompletions]