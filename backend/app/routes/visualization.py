from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload

from app.dependencies import get_async_db, get_current_organization
from app.core.auth import current_user as current_user_dep
from app.core.permissions_decorator import requires_permission

from app.models.user import User
from app.models.organization import Organization
from app.models.report import Report
from app.schemas.visualization_schema import VisualizationSchema, VisualizationUpdate
from app.schemas.view_schema import visualization_metadata
from app.services.step_service import StepService
from app.services.visualization_service import VisualizationService


router = APIRouter(prefix="/visualizations", tags=["visualizations"])
service = VisualizationService()


@router.get("/meta", response_model=dict)
async def get_visualization_meta():
    """Public metadata describing capabilities per visualization type.

    Frontend uses this to render relevant controls only.
    """
    return visualization_metadata()

async def _get_visualization_and_report(db: AsyncSession, visualization_id: str, organization: Organization):
    """Visualizations carry no org/owner columns, so the permission decorator
    can't scope them; resolve the parent report within the caller's org."""
    v = await service.get(db, visualization_id)
    report = None
    if v is not None:
        report = (await db.execute(
            select(Report).options(lazyload("*")).where(
                Report.id == v.report_id,
                Report.organization_id == organization.id,
            )
        )).scalar_one_or_none()
    if v is None or report is None:
        raise HTTPException(status_code=404, detail="Visualization not found")
    return v, report


@router.get("/{visualization_id}", response_model=VisualizationSchema)
@requires_permission('view_reports')
async def get_visualization(
    visualization_id: str,
    current_user: User = Depends(current_user_dep),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    v, report = await _get_visualization_and_report(db, visualization_id, organization)
    # Same gate as reading the report's steps: owner, org admin, project
    # collaborator, or a shared dashboard that shows this visualization's query.
    await StepService()._authorize_report_view(db, report, current_user, organization, query_id=v.query_id)
    return VisualizationSchema.model_validate(v)


@router.patch("/{visualization_id}", response_model=VisualizationSchema)
@requires_permission('update_reports')
async def patch_visualization(
    visualization_id: str,
    payload: VisualizationUpdate,
    current_user: User = Depends(current_user_dep),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    _, report = await _get_visualization_and_report(db, visualization_id, organization)
    if str(report.user_id) != str(current_user.id):
        raise HTTPException(status_code=403, detail="Only the owner can perform this action")
    v = await service.update(db, visualization_id, payload)
    if not v:
        raise HTTPException(status_code=404, detail="Visualization not found")
    return VisualizationSchema.model_validate(v)

