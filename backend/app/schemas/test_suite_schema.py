from pydantic import BaseModel
from typing import Optional, Any, Dict, List, Union
from datetime import datetime
from app.schemas.test_expectations import ExpectationsSpec


class TestSuiteSchema(BaseModel):
    id: str
    organization_id: str
    name: str
    description: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TestSuiteCreate(BaseModel):
    name: str
    description: Optional[str] = None


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
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


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


class TestRunSchema(BaseModel):
    id: str
    suite_ids: Optional[str] = None
    requested_by_user_id: Optional[str] = None
    trigger_reason: Optional[str] = None
    title: Optional[str] = None
    status: str
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    summary_json: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TestRunCreate(BaseModel):
    case_ids: Optional[List[str]] = None
    trigger_reason: Optional[str] = "manual"




