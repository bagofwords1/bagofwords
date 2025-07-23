from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_db, get_async_db
from app.dependencies import get_current_organization

from typing import List
from app.services.report_service import ReportService
from app.schemas.report_schema import ReportSchema, ReportCreate, ReportUpdate, ReportListResponse
from app.models.user import User

from app.core.auth import current_user
from app.models.organization import Organization
from app.core.permissions_decorator import requires_permission
from app.models.report import Report

router = APIRouter(tags=["reports"])
report_service = ReportService()

@router.post("/reports", response_model=ReportSchema)
@requires_permission('create_reports')
async def create_report(
    report: ReportCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await report_service.create_report(db, report, current_user, organization)

@router.get("/reports", response_model=ReportListResponse)
@requires_permission('view_reports')
async def get_reports(
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(10, ge=1, le=100, description="Number of items per page"),
    filter: str = Query("my", description="Filter: 'my' or 'published'"),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await report_service.get_reports(db, current_user, organization, page, limit, filter)

@router.get("/reports/{report_id}", response_model=ReportSchema)
@requires_permission('view_reports', model=Report, owner_only=True, allow_public=True)
async def get_report(report_id: str, db: AsyncSession = Depends(get_async_db), current_user: User = Depends(current_user), organization: Organization = Depends(get_current_organization)):
    return await report_service.get_report(db, report_id, current_user, organization)

@router.put("/reports/{report_id}", response_model=ReportSchema)
@requires_permission('update_reports', model=Report, owner_only=True)
async def update_report(report_id: str, report: ReportUpdate, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await report_service.update_report(db, report_id, report, current_user, organization)

@router.delete("/reports/{report_id}", response_model=ReportSchema)
@requires_permission('delete_reports', model=Report, owner_only=True)
async def delete_report(report_id: str, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await report_service.archive_report(db, report_id, current_user, organization)

@router.post("/reports/{report_id}/rerun", response_model=ReportSchema)
@requires_permission('rerun_report_steps', model=Report, owner_only=True)
async def rerun_report(report_id: str, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await report_service.rerun_report_steps(db, report_id, current_user, organization)

@router.post("/reports/{report_id}/publish", response_model=ReportSchema)
@requires_permission('publish_reports', model=Report, owner_only=True)
async def publish_report(report_id: str, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await report_service.publish_report(db, report_id, current_user, organization)

@router.get("/r/{report_id}", response_model=ReportSchema)
async def get_public_report(report_id: str, db: AsyncSession = Depends(get_async_db)):
    return await report_service.get_public_report(db, report_id)

@router.post("/reports/{report_id}/schedule", response_model=ReportSchema)
@requires_permission('publish_reports', model=Report, owner_only=True)
async def schedule_report(report_id: str, cron_expression: str, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await report_service.set_report_schedule(db, report_id, cron_expression, current_user, organization)
