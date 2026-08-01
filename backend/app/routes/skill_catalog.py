from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any, Dict, List

from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.dependencies import get_async_db, get_current_organization
from app.errors import AppError, ErrorCode
from app.models.organization import Organization
from app.models.user import User
from app.services.skills_catalog_service import skills_catalog_service

router = APIRouter(tags=["skills"])


@router.get("/skills/catalog", response_model=List[Dict[str, Any]])
async def list_skills_catalog(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Pre-built skills available to install, with per-org installed state.

    Readable by every member: the catalog is a menu, not org configuration.
    Installing is what changes shared context, and that is gated below.
    """
    return await skills_catalog_service.list_with_state(db, organization)


@router.post("/skills/catalog/{key}/install", response_model=Dict[str, Any])
@requires_permission('manage_instructions')
async def install_skill(
    key: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Install a pre-built skill as a global instruction with kind='skill'.

    Gated on `manage_instructions` because an installed skill is advertised to
    every agent in the organization.
    """
    try:
        return await skills_catalog_service.install(db, key, current_user, organization)
    except ValueError as e:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, str(e))
