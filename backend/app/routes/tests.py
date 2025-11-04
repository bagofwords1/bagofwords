from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List, Optional

from app.dependencies import get_async_db, get_current_organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.models.organization import Organization
from app.models.user import User
from app.models.test_suite import TestSuite, TestCase, TestRun, TestResult
from app.schemas.test_suite_schema import (
    TestSuiteSchema,
    TestSuiteCreate,
    TestSuiteUpdate,
    TestCaseSchema,
    TestCaseCreate,
    TestCaseUpdate,
    TestRunSchema,
    TestResultSchema,
)
from app.services.test_suite_service import TestSuiteService
from app.services.test_case_service import TestCaseService
from app.services.test_run_service import TestRunService


router = APIRouter(prefix="/tests", tags=["tests"])

suite_service = TestSuiteService()
case_service = TestCaseService()
run_service = TestRunService()


# Suites
@router.post("/suites", response_model=TestSuiteSchema)
@requires_permission('manage_tests')
async def create_suite(payload: TestSuiteCreate, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    suite = await suite_service.create_suite(db, str(organization.id), payload.name, payload.description, payload.report_id)
    return suite


@router.get("/suites", response_model=List[TestSuiteSchema])
@requires_permission('manage_tests')
async def list_suites(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    suites = await suite_service.list_suites(db, str(organization.id), page, limit, search)
    return suites


@router.get("/suites/{suite_id}", response_model=TestSuiteSchema)
@requires_permission('manage_tests')
async def get_suite(suite_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await suite_service.get_suite(db, str(organization.id), suite_id)


@router.patch("/suites/{suite_id}", response_model=TestSuiteSchema)
@requires_permission('manage_tests')
async def update_suite(suite_id: str, payload: TestSuiteUpdate, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await suite_service.update_suite(db, str(organization.id), suite_id, payload.name, payload.description, payload.report_id)


@router.delete("/suites/{suite_id}")
@requires_permission('manage_tests')
async def delete_suite(suite_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    await suite_service.delete_suite(db, str(organization.id), suite_id)
    return {"status": "deleted"}


# Cases
@router.post("/suites/{suite_id}/cases", response_model=TestCaseSchema)
@requires_permission('manage_tests')
async def create_case(suite_id: str, payload: TestCaseCreate, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    case = await case_service.create_case(db, str(organization.id), suite_id, payload.name, payload.prompt_json, payload.expectations_json, payload.data_source_ids_json)
    return case


@router.get("/suites/{suite_id}/cases", response_model=List[TestCaseSchema])
@requires_permission('manage_tests')
async def list_cases(suite_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await case_service.list_cases(db, str(organization.id), suite_id)


@router.get("/cases/{case_id}", response_model=TestCaseSchema)
@requires_permission('manage_tests')
async def get_case(case_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await case_service.get_case(db, str(organization.id), case_id)


@router.patch("/cases/{case_id}", response_model=TestCaseSchema)
@requires_permission('manage_tests')
async def update_case(case_id: str, payload: TestCaseUpdate, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await case_service.update_case(db, str(organization.id), case_id, payload.name, payload.prompt_json, payload.expectations_json, payload.data_source_ids_json)


@router.delete("/cases/{case_id}")
@requires_permission('manage_tests')
async def delete_case(case_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    await case_service.delete_case(db, str(organization.id), case_id)
    return {"status": "deleted"}


# Runs
@router.post("/suites/{suite_id}/runs", response_model=TestRunSchema)
@requires_permission('manage_tests')
async def run_suite(suite_id: str, background: bool = Query(True), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), user: User = Depends(current_user)):
    run = await run_service.run_suite(db, organization, user, suite_id, background=background)
    return run


@router.get("/runs", response_model=List[TestRunSchema])
@requires_permission('manage_tests')
async def list_runs(
    suite_id: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    runs = await run_service.list_runs(db, str(organization.id), suite_id=suite_id, status=status, page=page, limit=limit)
    return runs


@router.get("/runs/{run_id}", response_model=TestRunSchema)
@requires_permission('manage_tests')
async def get_run(run_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await run_service.get_run(db, str(organization.id), run_id)


@router.get("/runs/{run_id}/results", response_model=List[TestResultSchema])
@requires_permission('manage_tests')
async def list_results(run_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await run_service.list_results(db, str(organization.id), run_id)


@router.get("/results/{result_id}", response_model=TestResultSchema)
@requires_permission('manage_tests')
async def get_result(result_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await run_service.get_result(db, str(organization.id), result_id)


