from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List

from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.dependencies import get_async_db, get_current_organization
from app.ee.license import require_enterprise
from app.ee.audit.service import audit_service
from app.models.membership import Membership
from app.models.organization import Organization
from app.models.user import User
from app.schemas.usage_policy_schema import (
    EffectiveUsagePolicySchema,
    UsageDailySeriesSchema,
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


def _ensure_org_match(organization_id: str, organization: Organization) -> None:
    # Permission check authorizes against `organization` (resolved from header
    # / API key). Reject any request whose path org_id doesn't match so a
    # caller authorized for org A can't operate on org B's policies.
    if str(organization_id) != str(organization.id):
        raise HTTPException(status_code=403, detail="Organization mismatch")


@router.get("/organizations/{organization_id}/usage-policies", response_model=List[UsagePolicySchema])
@require_enterprise(feature="usage_limits")
@requires_permission("manage_settings")
async def list_usage_policies(
    organization_id: str,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    _ensure_org_match(organization_id, organization)
    return await usage_policy_service.list_policies(db, organization_id)


@router.post("/organizations/{organization_id}/usage-policies", response_model=UsagePolicySchema)
@require_enterprise(feature="usage_limits")
@requires_permission("manage_settings")
async def create_usage_policy(
    organization_id: str,
    data: UsagePolicyCreate,
    request: Request,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    _ensure_org_match(organization_id, organization)
    policy = await usage_policy_service.create_policy(db, organization_id, data)
    try:
        await audit_service.log(
            db=db, organization_id=organization_id, action="usage_policy.created",
            user_id=current_user.id, resource_type="usage_policy", resource_id=policy.id,
            details={"name": getattr(policy, "name", None)}, request=request,
        )
    except Exception:
        pass
    return policy


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
    _ensure_org_match(organization_id, organization)
    effective = await usage_policy_service.resolve_effective_limits(db, organization_id, user_id)
    return effective.to_schema()


@router.get("/organizations/{organization_id}/usage/daily", response_model=UsageDailySeriesSchema)
@require_enterprise(feature="usage_limits")
async def get_my_daily_usage(
    organization_id: str,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    """Current user's per-day usage since the start of the month (profile Usage tab).

    Self-serve: any member can read their own usage, so this checks membership
    directly instead of an admin permission.
    """
    _ensure_org_match(organization_id, organization)
    member = await db.execute(
        select(Membership).where(
            Membership.organization_id == str(organization.id),
            Membership.user_id == str(current_user.id),
            Membership.deleted_at.is_(None),
        )
    )
    if member.scalar_one_or_none() is None:
        raise HTTPException(status_code=403, detail="Not a member of this organization")
    return await usage_policy_service.get_user_daily_usage(
        db, str(organization.id), str(current_user.id)
    )


@router.put(
    "/organizations/{organization_id}/usage-policy-assignments/principal",
    response_model=UsagePolicyPrincipalAssignmentResult,
)
@require_enterprise(feature="usage_limits")
@requires_permission("manage_settings")
async def set_principal_usage_policy(
    organization_id: str,
    data: UsagePolicyPrincipalAssignmentUpdate,
    request: Request,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    _ensure_org_match(organization_id, organization)
    result = await usage_policy_service.set_principal_policy(
        db,
        organization_id,
        principal_type=data.principal_type,
        principal_id=data.principal_id,
        policy_id=data.policy_id,
    )
    try:
        await audit_service.log(
            db=db, organization_id=organization_id, action="usage_policy.assigned",
            user_id=current_user.id, resource_type="usage_policy", resource_id=data.policy_id,
            details={"principal_type": data.principal_type, "principal_id": data.principal_id,
                     "policy_id": data.policy_id}, request=request,
        )
    except Exception:
        pass
    return result


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
    _ensure_org_match(organization_id, organization)
    return await usage_policy_service.get_policy(db, organization_id, policy_id)


@router.put("/organizations/{organization_id}/usage-policies/{policy_id}", response_model=UsagePolicySchema)
@require_enterprise(feature="usage_limits")
@requires_permission("manage_settings")
async def update_usage_policy(
    organization_id: str,
    policy_id: str,
    data: UsagePolicyUpdate,
    request: Request,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    _ensure_org_match(organization_id, organization)
    try:
        policy = await usage_policy_service.update_policy(db, organization_id, policy_id, data)
    except UsageLimitExceeded as exc:
        raise _quota_http_error(exc)
    try:
        await audit_service.log(
            db=db, organization_id=organization_id, action="usage_policy.updated",
            user_id=current_user.id, resource_type="usage_policy", resource_id=policy_id,
            details={"fields": list(data.dict(exclude_unset=True).keys())}, request=request,
        )
    except Exception:
        pass
    return policy


@router.delete("/organizations/{organization_id}/usage-policies/{policy_id}", status_code=204)
@require_enterprise(feature="usage_limits")
@requires_permission("manage_settings")
async def delete_usage_policy(
    organization_id: str,
    policy_id: str,
    request: Request,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    _ensure_org_match(organization_id, organization)
    await usage_policy_service.delete_policy(db, organization_id, policy_id)
    try:
        await audit_service.log(
            db=db, organization_id=organization_id, action="usage_policy.deleted",
            user_id=current_user.id, resource_type="usage_policy", resource_id=policy_id,
            request=request,
        )
    except Exception:
        pass
