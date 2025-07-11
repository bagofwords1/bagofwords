from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional

from app.dependencies import get_async_db
from app.models.user import User
from app.models.organization import Organization
from app.core.auth import current_user
from app.dependencies import get_current_organization
from app.core.permissions_decorator import requires_permission
from app.services.external_user_mapping_service import ExternalUserMappingService
from app.schemas.external_user_mapping_schema import (
    ExternalUserMappingCreate,
    ExternalUserMappingUpdate,
    ExternalUserMappingSchema
)
from app.models.external_user_mapping import ExternalUserMapping

router = APIRouter(tags=["organization_settings"])
external_user_mapping_service = ExternalUserMappingService()

@router.get("/settings/integrations/{platform_id}/users", response_model=List[ExternalUserMappingSchema])
@requires_permission('view_organization_members')
async def get_integration_users(
    platform_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Get all users for a specific integration"""
    return await external_user_mapping_service.get_mappings_by_platform(
        db, organization, platform_id
    )

@router.post("/settings/integrations/{platform_id}/users", response_model=ExternalUserMappingSchema)
@requires_permission('add_organization_members')
async def create_integration_user(
    platform_id: str,
    mapping_data: ExternalUserMappingCreate,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Create a new user mapping for an integration"""
    return await external_user_mapping_service.create_mapping(
        db, organization, mapping_data
    )

@router.put("/settings/integrations/{platform_id}/users/{mapping_id}", response_model=ExternalUserMappingSchema)
@requires_permission('update_organization_members')
async def update_integration_user(
    platform_id: str,
    mapping_id: str,
    mapping_data: ExternalUserMappingUpdate,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Update a user mapping for an integration"""
    return await external_user_mapping_service.update_mapping(
        db, mapping_id, mapping_data, organization
    )

@router.delete("/settings/integrations/{platform_id}/users/{mapping_id}")
@requires_permission('remove_organization_members')
async def delete_integration_user(
    platform_id: str,
    mapping_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Delete a user mapping for an integration"""
    return await external_user_mapping_service.delete_mapping(
        db, mapping_id, organization
    )

@router.post("/settings/integrations/{platform_id}/users/{mapping_id}/verify", response_model=dict)
@requires_permission('update_organization_members')
async def generate_verification_token(
    platform_id: str,
    mapping_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Generate a verification token for a user mapping"""
    token = await external_user_mapping_service.generate_verification_token(
        db, mapping_id, organization
    )
    return {"verification_token": token}

# Public endpoint for verification (no auth required initially)
@router.get("/settings/integrations/verify/{token}")
async def verify_integration_user_token(
    token: str,
    db: AsyncSession = Depends(get_async_db)
):
    """Show verification page - user needs to sign in"""
    # Verify token is valid and not expired
    mapping = await external_user_mapping_service.get_mapping_by_token(db, token)
    
    if not mapping:
        raise HTTPException(status_code=400, detail="Invalid or expired verification token")
    
    # Return the verification page URL or redirect to sign-in
    # This would be handled by your frontend
    return {
        "token": token,
        "verification_url": f"/verify-integration?token={token}"
    }

# Protected endpoint to complete verification after sign-in
@router.post("/settings/integrations/verify/{token}/complete")
async def complete_verification(
    token: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db)
):
    """Complete verification after user signs in"""
    return await external_user_mapping_service.complete_verification(
        db, token, current_user
    ) 