from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List

from app.dependencies import get_async_db
from app.models.user import User
from app.models.organization import Organization
from app.core.auth import current_user
from app.dependencies import get_current_organization
from app.core.permissions_decorator import requires_permission
from app.services.external_platform_service import ExternalPlatformService
from app.schemas.external_platform_schema import (
    ExternalPlatformCreate,
    ExternalPlatformUpdate,
    ExternalPlatformSchema,
    SlackConfig
)
from app.models.external_platform import ExternalPlatform

router = APIRouter(tags=["organization_settings"])
external_platform_service = ExternalPlatformService()

@router.get("/settings/integrations", response_model=List[ExternalPlatformSchema])
@requires_permission('view_organization_settings')
async def get_integrations(
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Get all integrations for an organization"""
    return await external_platform_service.get_platforms(db, organization)

@router.get("/settings/integrations/{platform_id}", response_model=ExternalPlatformSchema)
@requires_permission('view_organization_settings', model=ExternalPlatform)
async def get_integration(
    platform_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Get a specific integration"""
    platform = await external_platform_service.get_platform_by_id(
        db, platform_id, organization
    )
    return ExternalPlatformSchema.from_orm(platform)

@router.put("/settings/integrations/{platform_id}", response_model=ExternalPlatformSchema)
@requires_permission('manage_organization_settings', model=ExternalPlatform)
async def update_integration(
    platform_id: str,
    platform_data: ExternalPlatformUpdate,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Update an integration"""
    return await external_platform_service.update_platform(
        db, platform_id, platform_data, organization
    )

@router.delete("/settings/integrations/{platform_id}")
@requires_permission('manage_organization_settings', model=ExternalPlatform)
async def delete_integration(
    platform_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Delete an integration"""
    return await external_platform_service.delete_platform(
        db, platform_id, organization
    )

@router.post("/settings/integrations/{platform_id}/test", response_model=dict)
@requires_permission('view_organization_settings', model=ExternalPlatform)
async def test_integration(
    platform_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Test connection to an integration"""
    return await external_platform_service.test_platform_connection(
        db, platform_id, organization
    )

@router.post("/settings/integrations/slack", response_model=ExternalPlatformSchema)
@requires_permission('manage_organization_settings')
async def create_slack_integration(
    data: SlackConfig,  # <-- Use SlackConfig here
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Create a new Slack integration"""
    return await external_platform_service.create_slack_platform(
        db, organization, data.bot_token, data.signing_secret, current_user
    )