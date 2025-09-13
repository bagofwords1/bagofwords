from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_async_db, get_current_organization
from app.core.auth import current_user as current_user_dep
from app.core.permissions_decorator import requires_permission

from app.models.user import User
from app.models.organization import Organization
from app.models.visualization import Visualization as VisualizationModel
from app.schemas.visualization_schema import VisualizationSchema, VisualizationUpdate
from app.schemas.view_schema import visualization_metadata
from app.services.visualization_service import VisualizationService


router = APIRouter(prefix="/visualizations", tags=["visualizations"])
service = VisualizationService()


@router.get("/meta", response_model=dict)
async def get_visualization_meta():
    """Public metadata describing capabilities per visualization type.

    Frontend uses this to render relevant controls only.
    """
    return visualization_metadata()

@router.get("/{visualization_id}", response_model=VisualizationSchema)
@requires_permission('view_reports', model=VisualizationModel, owner_only=True, allow_public=True)
async def get_visualization(
    visualization_id: str,
    current_user: User = Depends(current_user_dep),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    v = await service.get(db, visualization_id)
    if not v:
        raise HTTPException(status_code=404, detail="Visualization not found")
    return VisualizationSchema.model_validate(v)


@router.patch("/{visualization_id}", response_model=VisualizationSchema)
@requires_permission('update_reports', model=VisualizationModel, owner_only=True)
async def patch_visualization(
    visualization_id: str,
    payload: VisualizationUpdate,
    current_user: User = Depends(current_user_dep),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    v = await service.update(db, visualization_id, payload)
    if not v:
        raise HTTPException(status_code=404, detail="Visualization not found")
    return VisualizationSchema.model_validate(v)

