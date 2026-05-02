from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List

from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.dependencies import get_async_db, get_current_organization
from app.ee.license import require_enterprise
from app.models.organization import Organization
from app.models.user import User
from app.schemas.usage_policy_schema import (
    EffectiveUsagePolicySchema,
    UsagePolicyCreate,
    UsagePolicyPrincipalAssignmentResult,
    UsagePolicyPrincipalAssignmentUpdate,
    UsagePolicySchema,
    UsagePolicyUpdate,
)
from app.services.usage_policy_service import UsageLimitExceeded, usage_policy_service


router = APIRouter(tags=["usage_limits"])


def _quota_http_error(exc: UsageLimitExceeded) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail={
            "message": exc.detail,
            "metric": exc.metric,
            "limit": exc.limit,
            "used": exc.used,
            "requested": exc.requested,
        },
    )


@router.get("/organizations/{organization_id}/usage-policies", response_model=List[UsagePolicySchema])
@require_enterprise(feature="usage_limits")
@requires_permission("manage_settings")
async def list_usage_policies(
    organization_id: str,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    return await usage_policy_service.list_policies(db, organization_id)


@router.post("/organizations/{organization_id}/usage-policies", response_model=UsagePolicySchema)
@require_enterprise(feature="usage_limits")
@requires_permission("manage_settings")
async def create_usage_policy(
    organization_id: str,
    data: UsagePolicyCreate,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    return await usage_policy_service.create_policy(db, organization_id, data)


@router.get("/organizations/{organization_id}/usage-policies/effective/{user_id}", response_model=EffectiveUsagePolicySchema)
@require_enterprise(feature="usage_limits")
@requires_permission("manage_settings")
async def get_effective_usage_policy(
    organization_id: str,
    user_id: str,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    effective = await usage_policy_service.resolve_effective_limits(db, organization_id, user_id)
    return effective.to_schema()


@router.put(
    "/organizations/{organization_id}/usage-policy-assignments/principal",
    response_model=UsagePolicyPrincipalAssignmentResult,
)
@require_enterprise(feature="usage_limits")
@requires_permission("manage_settings")
async def set_principal_usage_policy(
    organization_id: str,
    data: UsagePolicyPrincipalAssignmentUpdate,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    return await usage_policy_service.set_principal_policy(
        db,
        organization_id,
        principal_type=data.principal_type,
        principal_id=data.principal_id,
        policy_id=data.policy_id,
    )


@router.get("/organizations/{organization_id}/usage-policies/{policy_id}", response_model=UsagePolicySchema)
@require_enterprise(feature="usage_limits")
@requires_permission("manage_settings")
async def get_usage_policy(
    organization_id: str,
    policy_id: str,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    return await usage_policy_service.get_policy(db, organization_id, policy_id)


@router.put("/organizations/{organization_id}/usage-policies/{policy_id}", response_model=UsagePolicySchema)
@require_enterprise(feature="usage_limits")
@requires_permission("manage_settings")
async def update_usage_policy(
    organization_id: str,
    policy_id: str,
    data: UsagePolicyUpdate,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    try:
        return await usage_policy_service.update_policy(db, organization_id, policy_id, data)
    except UsageLimitExceeded as exc:
        raise _quota_http_error(exc)


@router.delete("/organizations/{organization_id}/usage-policies/{policy_id}", status_code=204)
@require_enterprise(feature="usage_limits")
@requires_permission("manage_settings")
async def delete_usage_policy(
    organization_id: str,
    policy_id: str,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    await usage_policy_service.delete_policy(db, organization_id, policy_id)
