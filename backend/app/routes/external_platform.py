from fastapi import APIRouter, Depends, HTTPException, Request
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
    SlackConfig,
    TeamsConfig,
    WhatsAppConfig,
)
from app.models.external_platform import ExternalPlatform
from app.ee.audit.service import audit_service

router = APIRouter(tags=["organization_settings"])
external_platform_service = ExternalPlatformService()

@router.get("/settings/integrations", response_model=List[ExternalPlatformSchema])
@requires_permission('manage_settings')
async def get_integrations(
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Get all integrations for an organization"""
    return await external_platform_service.get_platforms(db, organization)

@router.get("/settings/integrations/{platform_id}", response_model=ExternalPlatformSchema)
@requires_permission('manage_settings', model=ExternalPlatform)
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
@requires_permission('manage_settings', model=ExternalPlatform)
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
@requires_permission('manage_settings', model=ExternalPlatform)
async def delete_integration(
    platform_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Delete an integration"""
    result = await external_platform_service.delete_platform(
        db, platform_id, organization
    )
    try:
        await audit_service.log(
            db=db,
            organization_id=organization.id,
            action="integration.deleted",
            user_id=current_user.id,
            resource_type="integration",
            resource_id=platform_id,
            request=request,
        )
    except Exception:
        pass
    return result

@router.post("/settings/integrations/{platform_id}/test", response_model=dict)
@requires_permission('manage_settings', model=ExternalPlatform)
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
@requires_permission('manage_settings')
async def create_slack_integration(
    data: SlackConfig,
    request: Request,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Create a new Slack integration"""
    result = await external_platform_service.create_slack_platform(
        db, organization, data.bot_token, data.signing_secret, current_user
    )
    try:
        await audit_service.log(
            db=db,
            organization_id=organization.id,
            action="integration.created",
            user_id=current_user.id,
            resource_type="integration",
            resource_id=result.id if hasattr(result, "id") else None,
            details={"type": "slack"},
            request=request,
        )
    except Exception:
        pass
    return result

@router.post("/settings/integrations/whatsapp", response_model=ExternalPlatformSchema)
@requires_permission('manage_settings')
async def create_whatsapp_integration(
    data: WhatsAppConfig,
    request: Request,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Create a new WhatsApp integration"""
    result = await external_platform_service.create_whatsapp_platform(
        db,
        organization,
        data.access_token,
        data.phone_number_id,
        data.waba_id,
        data.app_secret,
        data.verify_token,
        current_user,
    )
    try:
        await audit_service.log(
            db=db,
            organization_id=organization.id,
            action="integration.created",
            user_id=current_user.id,
            resource_type="integration",
            resource_id=result.id if hasattr(result, "id") else None,
            details={"type": "whatsapp"},
            request=request,
        )
    except Exception:
        pass
    return result

@router.post("/settings/integrations/teams", response_model=ExternalPlatformSchema)
@requires_permission('manage_settings')
async def create_teams_integration(
    data: TeamsConfig,
    request: Request,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Create a new Teams integration"""
    result = await external_platform_service.create_teams_platform(
        db, organization, data.app_id, data.client_secret, data.tenant_id, current_user
    )
    try:
        await audit_service.log(
            db=db,
            organization_id=organization.id,
            action="integration.created",
            user_id=current_user.id,
            resource_type="integration",
            resource_id=result.id if hasattr(result, "id") else None,
            details={"type": "teams"},
            request=request,
        )
    except Exception:
        pass
    return result