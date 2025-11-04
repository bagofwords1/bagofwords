from pydantic import BaseModel
from typing import Optional, Any, Dict, List
from datetime import datetime


class TestSuiteSchema(BaseModel):
    id: str
    organization_id: str
    name: str
    description: Optional[str] = None
    report_id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TestSuiteCreate(BaseModel):
    name: str
    description: Optional[str] = None
    report_id: str


class TestSuiteUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    report_id: Optional[str] = None


class TestCaseSchema(BaseModel):
    id: str
    suite_id: str
    name: str
    prompt_json: Dict[str, Any]
    expectations_json: Dict[str, Any]
    data_source_ids_json: Optional[List[str]] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TestCaseCreate(BaseModel):
    name: str
    prompt_json: Dict[str, Any]
    expectations_json: Dict[str, Any]
    data_source_ids_json: Optional[List[str]] = None


class TestCaseUpdate(BaseModel):
    name: Optional[str] = None
    prompt_json: Optional[Dict[str, Any]] = None
    expectations_json: Optional[Dict[str, Any]] = None
    data_source_ids_json: Optional[List[str]] = None


class TestRunSchema(BaseModel):
    id: str
    suite_id: str
    requested_by_user_id: Optional[str] = None
    trigger_reason: Optional[str] = None
    status: str
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    summary_json: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TestResultSchema(BaseModel):
    id: str
    run_id: str
    case_id: str
    head_completion_id: str
    status: str
    failure_reason: Optional[str] = None
    agent_execution_id: Optional[str] = None
    diffs_json: Optional[List[Dict[str, Any]]] = None
    metrics_json: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


