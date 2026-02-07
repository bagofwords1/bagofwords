from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_db, get_async_db
from app.dependencies import get_current_organization
from app.ee.audit.service import audit_service

from typing import List
from app.services.report_service import ReportService
from app.services.dashboard_layout_service import DashboardLayoutService
from app.schemas.report_schema import ReportSchema, ReportCreate, ReportUpdate, ReportListResponse
from app.schemas.dashboard_layout_version_schema import (
    DashboardLayoutVersionSchema,
    DashboardLayoutVersionCreate,
    DashboardLayoutVersionUpdate,
    DashboardLayoutBlocksPatch,
)
from app.models.user import User

from app.core.auth import current_user
from app.models.organization import Organization
from app.core.permissions_decorator import requires_permission
from app.models.report import Report

router = APIRouter(tags=["reports"])
report_service = ReportService()
layout_service = DashboardLayoutService()

@router.post("/reports", response_model=ReportSchema)
@requires_permission('create_reports')
async def create_report(
    report: ReportCreate,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    result = await report_service.create_report(db, report, current_user, organization)
    await audit_service.log(
        db=db,
        organization_id=organization.id,
        action="report.created",
        user_id=current_user.id,
        resource_type="report",
        resource_id=result.id,
        details={"title": result.title},
        request=request,
    )
    return result

@router.get("/reports", response_model=ReportListResponse)
@requires_permission('view_reports')
async def get_reports(
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(10, ge=1, le=100, description="Number of items per page"),
    filter: str = Query("my", description="Filter: 'my' or 'published'"),
    search: str | None = Query(None, description="Optional search term for report title"),
    scheduled: bool | None = Query(None, description="Filter by scheduled reports (true = only scheduled, false = only non-scheduled)"),
    status: str | None = Query(None, description="Filter by status: 'draft' or 'published'"),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await report_service.get_reports(db, current_user, organization, page, limit, filter, search, scheduled, status)

@router.put("/reports/{report_id}", response_model=ReportSchema)
@requires_permission('update_reports', model=Report, owner_only=True)
async def update_report(report_id: str, report: ReportUpdate, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await report_service.update_report(db, report_id, report, current_user, organization)

@router.get("/reports/{report_id}", response_model=ReportSchema)
@requires_permission('view_reports', model=Report, owner_only=True, allow_public=True)
async def get_report(report_id: str, db: AsyncSession = Depends(get_async_db), current_user: User = Depends(current_user), organization: Organization = Depends(get_current_organization)):
    return await report_service.get_report(db, report_id, current_user, organization)

@router.delete("/reports/{report_id}", response_model=ReportSchema)
@requires_permission('delete_reports', model=Report, owner_only=True)
async def delete_report(
    report_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    result = await report_service.archive_report(db, report_id, current_user, organization)
    await audit_service.log(
        db=db,
        organization_id=organization.id,
        action="report.deleted",
        user_id=current_user.id,
        resource_type="report",
        resource_id=report_id,
        details={"title": result.title},
        request=request,
    )
    return result


@router.post("/reports/bulk/archive")
@requires_permission('delete_reports')
async def bulk_archive_reports(
    report_ids: List[str],
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """
    Archive multiple reports in a single operation.
    Only reports owned by the current user (or otherwise deletable per service rules) will be archived.
    """
    return await report_service.bulk_archive_reports(db, report_ids, current_user, organization)

@router.post("/reports/{report_id}/rerun", response_model=ReportSchema)
@requires_permission('rerun_report_steps', model=Report, owner_only=True)
async def rerun_report(report_id: str, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await report_service.rerun_report_steps(db, report_id, current_user, organization)

@router.post("/reports/{report_id}/publish", response_model=ReportSchema)
@requires_permission('publish_reports', model=Report, owner_only=True)
async def publish_report(
    report_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    result = await report_service.publish_report(db, report_id, current_user, organization)
    await audit_service.log(
        db=db,
        organization_id=organization.id,
        action="report.published",
        user_id=current_user.id,
        resource_type="report",
        resource_id=report_id,
        details={"title": result.title, "status": result.status},
        request=request,
    )
    return result

@router.post("/reports/{report_id}/conversation-share")
@requires_permission('publish_reports', model=Report, owner_only=True)
async def toggle_conversation_share(report_id: str, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    """Toggle conversation sharing for a report. Returns enabled status and share token."""
    return await report_service.toggle_conversation_share(db, report_id, current_user, organization)

@router.get("/r/{report_id}", response_model=ReportSchema)
async def get_public_report(report_id: str, db: AsyncSession = Depends(get_async_db)):
    return await report_service.get_public_report(db, report_id)

@router.get("/c/{token}")
async def get_public_conversation(
    token: str,
    limit: int = 10,
    before: str | None = None,
    db: AsyncSession = Depends(get_async_db)
):
    """Public endpoint to fetch a shared conversation by its token. Supports pagination."""
    return await report_service.get_public_conversation(db, token, limit=limit, before=before)


# --- Public Query/Step Routes (for published reports) ---

from app.schemas.query_schema import PublicQuerySchema
from app.schemas.step_schema import PublicStepSchema


@router.get("/r/{report_id}/queries", response_model=List[PublicQuerySchema])
async def get_public_queries(
    report_id: str,
    artifact_id: str | None = Query(None, description="Filter queries to only those used by this artifact"),
    db: AsyncSession = Depends(get_async_db)
):
    """Get queries for a published report (no auth required).

    If artifact_id is provided, only returns queries for visualizations used by that artifact.
    """
    return await report_service.get_public_queries(db, report_id, artifact_id=artifact_id)


@router.get("/r/{report_id}/queries/{query_id}/step", response_model=PublicStepSchema)
async def get_public_query_step(
    report_id: str,
    query_id: str,
    db: AsyncSession = Depends(get_async_db)
):
    """Get the default step for a query in a published report (no auth required)."""
    return await report_service.get_public_step(db, report_id, query_id)


# --- Public Artifact Routes (for published reports) ---

from app.schemas.artifact_schema import ArtifactListSchema, ArtifactSchema


@router.get("/r/{report_id}/artifacts", response_model=List[ArtifactListSchema])
async def get_public_artifacts(report_id: str, db: AsyncSession = Depends(get_async_db)):
    """List artifacts for a published report (no auth required)."""
    return await report_service.get_public_artifacts(db, report_id)


@router.get("/r/{report_id}/artifacts/{artifact_id}", response_model=ArtifactSchema)
async def get_public_artifact(
    report_id: str,
    artifact_id: str,
    db: AsyncSession = Depends(get_async_db)
):
    """Get a specific artifact for a published report (no auth required)."""
    return await report_service.get_public_artifact(db, report_id, artifact_id)


@router.post("/reports/{report_id}/schedule", response_model=ReportSchema)
@requires_permission('publish_reports', model=Report, owner_only=True)
async def schedule_report(report_id: str, cron_expression: str, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await report_service.set_report_schedule(db, report_id, cron_expression, current_user, organization)

# --- Training Mode Instructions ---

@router.get("/reports/{report_id}/instructions")
@requires_permission('view_reports', model=Report, owner_only=True)
async def get_report_instructions(
    report_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Get all instructions created during this report's training sessions.

    Returns instructions that were created by the AI agent during training mode.
    These are the instructions that were auto-generated and published to the
    training session's build.
    """
    from app.services.instruction_service import InstructionService
    instruction_service = InstructionService()
    return await instruction_service.get_instructions_by_report(db, report_id, organization)

# --- Dashboard Layout Routes ---

@router.get("/reports/{report_id}/layouts", response_model=List[DashboardLayoutVersionSchema])
@requires_permission('view_reports', model=Report, owner_only=True)
async def list_layouts(report_id: str, hydrate: bool = False, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await layout_service.get_layouts_for_report(db, report_id, hydrate=hydrate)

@router.post("/reports/{report_id}/layouts", response_model=DashboardLayoutVersionSchema)
@requires_permission('update_reports', model=Report, owner_only=True)
async def create_layout(report_id: str, payload: DashboardLayoutVersionCreate, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    # Ensure payload.report_id matches route
    if payload.report_id != report_id:
        raise HTTPException(status_code=400, detail="report_id mismatch")
    return await layout_service.create_layout(db, payload)

@router.get("/reports/{report_id}/layouts/{layout_id}", response_model=DashboardLayoutVersionSchema)
@requires_permission('view_reports', model=Report, owner_only=True)
async def get_layout(report_id: str, layout_id: str, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    layout = await layout_service.get_layout(db, layout_id)
    if layout.report_id != report_id:
        raise HTTPException(status_code=404, detail="Layout not found for report")
    return layout

@router.patch("/reports/{report_id}/layouts/{layout_id}", response_model=DashboardLayoutVersionSchema)
@requires_permission('update_reports', model=Report, owner_only=True)
async def update_layout(report_id: str, layout_id: str, payload: DashboardLayoutVersionUpdate, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    layout = await layout_service.get_layout(db, layout_id)
    if layout.report_id != report_id:
        raise HTTPException(status_code=404, detail="Layout not found for report")
    return await layout_service.update_layout(db, layout_id, payload, current_user, organization)

@router.patch("/reports/{report_id}/layouts/active/blocks", response_model=DashboardLayoutVersionSchema)
@requires_permission('update_reports', model=Report, owner_only=True)
async def patch_active_layout_blocks(report_id: str, payload: DashboardLayoutBlocksPatch, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await layout_service.patch_active_layout_blocks(db, report_id, payload, current_user, organization)


@router.patch("/reports/{report_id}/layouts/{layout_id}/blocks", response_model=DashboardLayoutVersionSchema)
@requires_permission('update_reports', model=Report, owner_only=True)
async def patch_layout_blocks(report_id: str, layout_id: str, payload: DashboardLayoutBlocksPatch, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await layout_service.patch_layout_blocks(db, report_id, layout_id, payload, current_user, organization)

@router.post("/reports/{report_id}/layouts/{layout_id}/activate", response_model=DashboardLayoutVersionSchema)
@requires_permission('update_reports', model=Report, owner_only=True)
async def activate_layout(report_id: str, layout_id: str, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    layout = await layout_service.get_layout(db, layout_id)
    if layout.report_id != report_id:
        raise HTTPException(status_code=404, detail="Layout not found for report")
    return await layout_service.set_active_layout(db, report_id, layout_id)

# --- Public (read-only) Dashboard Layout Routes ---

@router.get("/r/{report_id}/layouts", response_model=List[DashboardLayoutVersionSchema])
async def get_public_layouts(report_id: str, hydrate: bool = False, db: AsyncSession = Depends(get_async_db)):
    from app.services.report_service import ReportService
    rs = ReportService()
    # Public service currently returns unhydrated; use private service for hydration
    if hydrate:
        return await layout_service.get_layouts_for_report(db, report_id, hydrate=True)
    return await rs.get_public_layouts(db, report_id)
