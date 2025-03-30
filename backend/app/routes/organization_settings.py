from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_async_db, get_current_organization
from app.models.user import User
from app.models.organization import Organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.services.organization_settings_service import OrganizationSettingsService
from app.schemas.organization_settings_schema import (
    OrganizationSettingsSchema,
    OrganizationSettingsUpdate
)

router = APIRouter(tags=["organization_settings"])
settings_service = OrganizationSettingsService()

@router.get("/organization/settings", response_model=OrganizationSettingsSchema)
@requires_permission('view_organization_settings')
async def get_organization_settings(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Get all settings for the organization"""
    return await settings_service.get_settings(db, organization, current_user)

@router.put("/organization/settings", response_model=OrganizationSettingsSchema)
@requires_permission('manage_organization_settings')
async def update_organization_settings(
    settings: OrganizationSettingsUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Update organization settings"""
    return await settings_service.update_settings(db, organization, current_user, settings)

@router.post("/organization/settings/agents/{agent_name}")
@requires_permission('manage_organization_settings')
async def update_agent_setting(
    agent_name: str,
    enabled: bool,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Enable/disable a specific agent"""
    return await settings_service.update_agent_setting(db, organization, current_user, agent_name, enabled) 