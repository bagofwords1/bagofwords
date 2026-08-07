from pydantic import BaseModel
from typing import Optional, Any, Dict, List, Union
from datetime import datetime
from app.schemas.test_expectations import ExpectationsSpec
from app.schemas.base import UTCDatetime, OptionalUTCDatetime


class ModelSummary(BaseModel):
    id: str
    model_id: str
    name: Optional[str] = None
    provider_name: Optional[str] = None
    provider_type: Optional[str] = None


class TestSuiteSchema(BaseModel):
    id: str
    organization_id: str
    name: str
    description: Optional[str] = None
    # The agent this suite lives under (None = org-wide). A HOME, not a scope:
    # it decides where the suite renders and where new cases default, and does
    # not constrain what a case inside may target.
    data_source_id: Optional[str] = None
    created_at: UTCDatetime
    updated_at: UTCDatetime

    class Config:
        from_attributes = True


class TestSuiteCreate(BaseModel):
    name: str
    description: Optional[str] = None
    data_source_id: Optional[str] = None


class TestSuiteUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class TestCaseSchema(BaseModel):
    id: str
    suite_id: str
    name: str
    prompt_json: Dict[str, Any]
    expectations_json: ExpectationsSpec
    data_source_ids_json: Optional[List[str]] = None
    status: str = "active"
    auto_generated: bool = False
    source_completion_id: Optional[str] = None
    source_agent_execution_id: Optional[str] = None
    source_feedback_id: Optional[str] = None
    # Derived, not persisted: helpful projection for UI fallbacks
    model_summary: Optional[ModelSummary] = None
    created_at: UTCDatetime
    updated_at: UTCDatetime

    class Config:
        from_attributes = True


class TestCaseStatusUpdate(BaseModel):
    status: str


class TestCaseCreate(BaseModel):
    name: str
    prompt_json: Dict[str, Any]
    expectations_json: ExpectationsSpec
    data_source_ids_json: Optional[List[str]] = None


class TestCaseUpdate(BaseModel):
    name: Optional[str] = None
    prompt_json: Optional[Dict[str, Any]] = None
    expectations_json: Optional[ExpectationsSpec] = None
    data_source_ids_json: Optional[List[str]] = None
    # Move a case between suites. A suite is a folder, so this needs authority
    # over the CASE only (already checked) — filing your own case into a suite
    # touches nothing that is already in it. Without this field a case's suite
    # was fixed at creation and could never be reorganized, by anyone.
    suite_id: Optional[str] = None


class TestRunCaseResultBrief(BaseModel):
    """Per-case status embedded in run listings so clients don't need one
    /runs/{id}/results request per listed run (the old N+1)."""
    case_id: str
    status: str


class TestRunSchema(BaseModel):
    id: str
    suite_ids: Optional[str] = None
    requested_by_user_id: Optional[str] = None
    trigger_reason: Optional[str] = None
    title: Optional[str] = None
    status: str
    started_at: OptionalUTCDatetime = None
    finished_at: OptionalUTCDatetime = None
    summary_json: Optional[Dict[str, Any]] = None
    # Build system
    build_id: Optional[str] = None
    build_number: Optional[int] = None
    # True when this response is an already-running identical run returned
    # instead of a duplicate (transient, never persisted).
    deduped: Optional[bool] = None
    # Populated by list_runs only (transient attribute, never persisted).
    case_results: Optional[List[TestRunCaseResultBrief]] = None
    created_at: UTCDatetime
    updated_at: UTCDatetime

    class Config:
        from_attributes = True


class TestRunCreate(BaseModel):
    case_ids: Optional[List[str]] = None
    trigger_reason: Optional[str] = "manual"
    # Build system: optionally specify which instruction build to use
    # If None, uses the current main build (is_main=True)
    build_id: Optional[str] = None




