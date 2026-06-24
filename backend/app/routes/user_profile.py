from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from typing import Optional
from pydantic import BaseModel, Field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.models.membership import Membership
from app.models.organization import Organization
from app.core.auth import current_user
from app.dependencies import get_async_db, get_current_organization
from app.schemas.user_profile_schema import UserProfileSchema
from app.schemas.organization_schema import OrganizationAndRoleSchema, MEMBERSHIP_NOTE_MAX_LENGTH
from app.services.organization_service import OrganizationService

router = APIRouter(tags=["users"])
organization_service = OrganizationService()


class UserInstructionsSchema(BaseModel):
    # The current user's per-organization note (membership.note). Surfaced to
    # the AI planner, so we reuse the same length cap as the members admin UI.
    note: Optional[str] = Field(default=None, max_length=MEMBERSHIP_NOTE_MAX_LENGTH)


async def _get_current_membership(
    db: AsyncSession, user: User, organization: Organization
) -> Optional[Membership]:
    result = await db.execute(
        select(Membership).where(
            Membership.user_id == user.id,
            Membership.organization_id == organization.id,
        )
    )
    return result.scalars().first()

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


@router.get("/users/me/instructions", response_model=UserInstructionsSchema)
async def get_my_instructions(
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Return the current user's custom instructions (their membership note)
    for the active organization."""
    membership = await _get_current_membership(db, current_user, organization)
    return UserInstructionsSchema(note=membership.note if membership else None)


@router.put("/users/me/instructions", response_model=UserInstructionsSchema)
async def update_my_instructions(
    payload: UserInstructionsSchema,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Update the current user's custom instructions for the active organization.
    Self-service: a user can always edit their own note regardless of role."""
    membership = await _get_current_membership(db, current_user, organization)
    if not membership:
        raise HTTPException(status_code=404, detail="Membership not found")

    note = (payload.note or "").strip()
    membership.note = note or None
    await db.commit()
    return UserInstructionsSchema(note=membership.note)
