"""
Demo Data Source Routes

Endpoints for listing and installing demo data sources.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List

from app.dependencies import get_async_db
from app.models.user import User
from app.core.auth import current_user
from app.models.organization import Organization
from app.dependencies import get_current_organization
from app.services.demo_data_source_service import DemoDataSourceService
from app.schemas.demo_data_source_schema import (
    DemoDataSourceListItem,
    DemoDataSourceInstallResponse,
)
from app.core.permissions_decorator import requires_permission

router = APIRouter(tags=["data_sources"])
demo_service = DemoDataSourceService()


@router.get("/data_sources/demos", response_model=List[DemoDataSourceListItem])
@requires_permission("create_data_source")
async def list_demo_data_sources(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """
    List all available demo data sources.
    
    Returns a list of demo databases that can be installed,
    along with their installation status for the current organization.
    """
    return await demo_service.list_demo_data_sources(db, organization)


@router.post(
    "/data_sources/demos/{demo_id}",
    response_model=DemoDataSourceInstallResponse,
)
@requires_permission("create_data_source")
async def install_demo_data_source(
    demo_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """
    Install a demo data source.
    
    Creates a new data source from the demo template.
    If already installed, returns the existing data source ID.
    """
    return await demo_service.install_demo_data_source(
        db=db,
        organization=organization,
        current_user=current_user,
        demo_id=demo_id,
    )

