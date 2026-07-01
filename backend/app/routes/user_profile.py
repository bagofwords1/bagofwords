import os
import hashlib
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from typing import Optional
from pydantic import BaseModel, Field
from PIL import Image

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.models.membership import Membership
from app.models.organization import Organization
from app.core.auth import current_user
from app.dependencies import get_async_db, get_current_organization, release_request_db
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

    # Build the (detached-safe) response, then release the pooled DB connection
    # before FastAPI serializes/sends it — whoami runs on every navigation, so
    # holding the connection through serialization is a real pool cost.
    payload = UserProfileSchema(
        **user_data,
        organizations=organizations
    )
    await release_request_db(db)
    return payload


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


_AVATAR_MAX_BYTES = 5 * 1024 * 1024  # 5 MB upload cap


class UserAvatarSchema(BaseModel):
    image_url: Optional[str] = None


@router.post("/users/me/avatar", response_model=UserAvatarSchema)
async def upload_my_avatar(
    avatar: UploadFile = File(...),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    """Upload the current user's profile image. The image is normalized to a
    square 256x256 PNG and served publicly via /api/users/avatar/{key}."""
    raw = await avatar.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(raw) > _AVATAR_MAX_BYTES:
        raise HTTPException(status_code=400, detail="Image is too large (max 5 MB)")

    try:
        image = Image.open(BytesIO(raw))
        image.load()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid image file")

    image = image.convert("RGBA")

    # Center-crop to a square, then resize to 256x256 for a consistent avatar.
    width, height = image.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    image = image.crop((left, top, left + side, top + side))
    image = image.resize((256, 256), Image.Resampling.LANCZOS)

    base_dir = os.path.abspath(os.path.join(os.getcwd(), "uploads", "avatars"))
    os.makedirs(base_dir, exist_ok=True)

    digest = hashlib.sha256(raw).hexdigest()[:16]
    filename = f"{current_user.id}-{digest}.png"
    file_path = os.path.join(base_dir, filename)

    buf = BytesIO()
    image.save(buf, format="PNG")
    with open(file_path, "wb") as f:
        f.write(buf.getvalue())

    image_url = f"/api/users/avatar/{filename}"
    # current_user is bound to the auth/user-manager session, not `db`, so we
    # update via a statement rather than mutating the ORM object (mirrors
    # _update_last_seen in app/core/auth.py).
    await db.execute(
        update(User).where(User.id == str(current_user.id)).values(image_url=image_url)
    )
    await db.commit()
    return UserAvatarSchema(image_url=image_url)


@router.delete("/users/me/avatar", response_model=UserAvatarSchema)
async def delete_my_avatar(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    """Remove the current user's avatar, reverting to the initial placeholder."""
    await db.execute(
        update(User).where(User.id == str(current_user.id)).values(image_url=None)
    )
    await db.commit()
    return UserAvatarSchema(image_url=None)
