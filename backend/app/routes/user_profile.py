from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from typing import Optional

from app.models.user import User
from app.core.auth import current_user

from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_async_db
from app.schemas.user_profile_schema import UserProfileSchema
from app.schemas.organization_schema import OrganizationAndRoleSchema
from app.services.organization_service import OrganizationService

router = APIRouter(tags=["users"])
organization_service = OrganizationService()

@router.get("/users/whoami", response_model=UserProfileSchema)
async def get_user_profile(current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db)):
    # Fetch organizations for the current user
    organizations = await organization_service.get_user_organizations(db, current_user)
    
    # Convert current_user to a dictionary
    user_data = current_user.dict() if hasattr(current_user, 'dict') else vars(current_user)
    
    # Return the user profile with formatted organizations
    return UserProfileSchema(
        **user_data,
        organizations=organizations
    )
