from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_async_db, get_current_organization
from app.models.user import User
from app.models.organization import Organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.schemas.onboarding_schema import OnboardingResponse, OnboardingUpdate
from app.services.onboarding_service import OnboardingService


router = APIRouter(tags=["onboarding"])
service = OnboardingService()


@router.get("/organization/onboarding", response_model=OnboardingResponse)
@requires_permission('view_organization_settings')
async def get_onboarding(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await service.get_onboarding(db, organization, current_user)


@router.put("/organization/onboarding", response_model=OnboardingResponse)
@requires_permission('manage_organization_settings')
async def update_onboarding(
    payload: OnboardingUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await service.update_onboarding(db, organization, current_user, payload)


