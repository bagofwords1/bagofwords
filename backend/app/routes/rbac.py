from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional

from fastapi import HTTPException

from app.dependencies import get_async_db, get_current_organization
from app.models.user import User
from app.models.organization import Organization
from app.models.resource_grant import ResourceGrant
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.core.permission_resolver import resolve_permissions, FULL_ADMIN
from sqlalchemy import select
from app.ee.license import require_enterprise
from app.ee.audit.service import audit_service
from app.services.rbac_service import rbac_service
from app.schemas.rbac_schema import (
    RoleCreate, RoleUpdate, RoleSchema,
    GroupCreate, GroupUpdate, GroupSchema, GroupMemberAdd, GroupMemberSchema,
    RoleAssignmentCreate, RoleAssignmentSchema,
    ResourceGrantCreate, ResourceGrantUpdate, ResourceGrantSchema,
)

from app.core.auth import forbid_service_account_principal

# Service accounts must never manage roles, groups, assignments, or grants —
# this is the self-escalation guard (a leaked SA key cannot grant itself more).
router = APIRouter(tags=["rbac"], dependencies=[Depends(forbid_service_account_principal)])


# ── Permission Registry ──────────────────────────────────────────────────

@router.get("/permissions/registry")
async def get_permissions_registry(current_user: User = Depends(current_user)):
    """Returns all available permission categories and resource permission options."""
    from app.core.permissions_registry import (
        PERMISSION_CATEGORIES, RESOURCE_PERMISSIONS,
        MERGED_CATEGORIES, RESOURCE_SCOPED_GROUPS,
    )
    return {
        "categories": PERMISSION_CATEGORIES,
        "resource_permissions": RESOURCE_PERMISSIONS,
        "merged_categories": MERGED_CATEGORIES,
        "resource_scoped_groups": RESOURCE_SCOPED_GROUPS,
    }


# ── Roles ────────────────────────────────────────────────────────────────

@router.get("/organizations/{organization_id}/roles", response_model=List[RoleSchema])
@requires_permission("view_members")
async def list_roles(
    organization_id: str,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    return await rbac_service.list_roles(db, organization_id)


@router.post("/organizations/{organization_id}/roles", response_model=RoleSchema)
@require_enterprise(feature="custom_roles")
@requires_permission("manage_members")
async def create_role(
    organization_id: str,
    data: RoleCreate,
    request: Request,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    role = await rbac_service.create_role(db, organization_id, data)
    try:
        await audit_service.log(
            db=db, organization_id=organization_id, action="role.created",
            user_id=current_user.id, resource_type="role", resource_id=role.id,
            details={"name": role.name, "permissions": data.permissions}, request=request,
        )
    except Exception:
        pass
    return role


@router.put("/organizations/{organization_id}/roles/{role_id}", response_model=RoleSchema)
@require_enterprise(feature="custom_roles")
@requires_permission("manage_members")
async def update_role(
    organization_id: str,
    role_id: str,
    data: RoleUpdate,
    request: Request,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    role = await rbac_service.update_role(db, organization_id, role_id, data)
    try:
        await audit_service.log(
            db=db, organization_id=organization_id, action="role.updated",
            user_id=current_user.id, resource_type="role", resource_id=role_id,
            details={"name": role.name,
                     "fields": list(data.dict(exclude_unset=True).keys())},
            request=request,
        )
    except Exception:
        pass
    return role


@router.delete("/organizations/{organization_id}/roles/{role_id}", status_code=204)
@require_enterprise(feature="custom_roles")
@requires_permission("manage_members")
async def delete_role(
    organization_id: str,
    role_id: str,
    request: Request,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    await rbac_service.delete_role(db, organization_id, role_id)
    try:
        await audit_service.log(
            db=db, organization_id=organization_id, action="role.deleted",
            user_id=current_user.id, resource_type="role", resource_id=role_id,
            request=request,
        )
    except Exception:
        pass


# ── Groups ───────────────────────────────────────────────────────────────

@router.get("/organizations/{organization_id}/groups", response_model=List[GroupSchema])
@require_enterprise(feature="custom_roles")
@requires_permission("view_members")
async def list_groups(
    organization_id: str,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    return await rbac_service.list_groups(db, organization_id)


@router.post("/organizations/{organization_id}/groups", response_model=GroupSchema)
@require_enterprise(feature="custom_roles")
@requires_permission("manage_members")
async def create_group(
    organization_id: str,
    data: GroupCreate,
    request: Request,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    group = await rbac_service.create_group(db, organization_id, data)
    try:
        await audit_service.log(
            db=db, organization_id=organization_id, action="group.created",
            user_id=current_user.id, resource_type="group", resource_id=group.id,
            details={"name": group.name}, request=request,
        )
    except Exception:
        pass
    return group


@router.put("/organizations/{organization_id}/groups/{group_id}", response_model=GroupSchema)
@require_enterprise(feature="custom_roles")
@requires_permission("manage_members")
async def update_group(
    organization_id: str,
    group_id: str,
    data: GroupUpdate,
    request: Request,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    group = await rbac_service.update_group(db, organization_id, group_id, data)
    try:
        await audit_service.log(
            db=db, organization_id=organization_id, action="group.updated",
            user_id=current_user.id, resource_type="group", resource_id=group_id,
            details={"fields": list(data.dict(exclude_unset=True).keys())}, request=request,
        )
    except Exception:
        pass
    return group


@router.delete("/organizations/{organization_id}/groups/{group_id}", status_code=204)
@require_enterprise(feature="custom_roles")
@requires_permission("manage_members")
async def delete_group(
    organization_id: str,
    group_id: str,
    request: Request,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    await rbac_service.delete_group(db, organization_id, group_id)
    try:
        await audit_service.log(
            db=db, organization_id=organization_id, action="group.deleted",
            user_id=current_user.id, resource_type="group", resource_id=group_id,
            request=request,
        )
    except Exception:
        pass


@router.get("/organizations/{organization_id}/groups/{group_id}/members", response_model=List[GroupMemberSchema])
@require_enterprise(feature="custom_roles")
@requires_permission("manage_members")
async def list_group_members(
    organization_id: str,
    group_id: str,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    return await rbac_service.list_group_members(db, organization_id, group_id)


@router.post("/organizations/{organization_id}/groups/{group_id}/members", status_code=201)
@require_enterprise(feature="custom_roles")
@requires_permission("manage_members")
async def add_group_member(
    organization_id: str,
    group_id: str,
    data: GroupMemberAdd,
    request: Request,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    await rbac_service.add_group_member(
        db, organization_id, group_id,
        user_id=data.user_id, membership_id=data.membership_id,
    )
    try:
        await audit_service.log(
            db=db, organization_id=organization_id, action="group.member_added",
            user_id=current_user.id, resource_type="group", resource_id=group_id,
            details={"user_id": data.user_id, "membership_id": data.membership_id},
            request=request,
        )
    except Exception:
        pass


@router.delete("/organizations/{organization_id}/groups/{group_id}/members/{principal_id}", status_code=204)
@require_enterprise(feature="custom_roles")
@requires_permission("manage_members")
async def remove_group_member(
    organization_id: str,
    group_id: str,
    principal_id: str,
    request: Request,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    await rbac_service.remove_group_member(db, organization_id, group_id, principal_id)
    try:
        await audit_service.log(
            db=db, organization_id=organization_id, action="group.member_removed",
            user_id=current_user.id, resource_type="group", resource_id=group_id,
            details={"principal_id": principal_id}, request=request,
        )
    except Exception:
        pass


# ── Role Assignments ─────────────────────────────────────────────────────

@router.get("/organizations/{organization_id}/role-assignments", response_model=List[RoleAssignmentSchema])
@requires_permission("view_members")
async def list_role_assignments(
    organization_id: str,
    principal_type: Optional[str] = Query(None),
    principal_id: Optional[str] = Query(None),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    return await rbac_service.list_role_assignments(db, organization_id, principal_type, principal_id)


@router.post("/organizations/{organization_id}/role-assignments", response_model=RoleAssignmentSchema)
@requires_permission("manage_members")
async def create_role_assignment(
    organization_id: str,
    data: RoleAssignmentCreate,
    request: Request,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    assignment = await rbac_service.create_role_assignment(db, organization_id, data)
    try:
        await audit_service.log(
            db=db, organization_id=organization_id, action="role.assigned",
            user_id=current_user.id, resource_type="role_assignment", resource_id=assignment.id,
            details={"role_id": data.role_id, "principal_type": data.principal_type,
                     "principal_id": data.principal_id}, request=request,
        )
    except Exception:
        pass
    return assignment


@router.delete("/organizations/{organization_id}/role-assignments/{assignment_id}", status_code=204)
@requires_permission("manage_members")
async def delete_role_assignment(
    organization_id: str,
    assignment_id: str,
    request: Request,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    await rbac_service.delete_role_assignment(db, organization_id, assignment_id)
    try:
        await audit_service.log(
            db=db, organization_id=organization_id, action="role.assignment_revoked",
            user_id=current_user.id, resource_type="role_assignment", resource_id=assignment_id,
            request=request,
        )
    except Exception:
        pass


# ── Resource Grants ──────────────────────────────────────────────────────

async def _require_resource_manage(
    db: AsyncSession, user: User, org_id: str, resource_type: str, resource_id: str
) -> None:
    """Authorize a resource-grant mutation: caller must hold `manage` on the
    target resource (or be a full org admin). Org-level `manage_members` is
    deliberately NOT sufficient — granting per-resource access requires
    per-resource authority.
    """
    from app.core.permission_resolver import principal_belongs_to_org
    if not await principal_belongs_to_org(db, user, org_id):
        raise HTTPException(status_code=403, detail="User is not a member of this organization")

    resolved = await resolve_permissions(db, str(user.id), str(org_id))
    if FULL_ADMIN in resolved.org_permissions:
        return
    if resolved.has_resource_permission(resource_type, str(resource_id), "manage"):
        return
    raise HTTPException(
        status_code=403,
        detail=f"Requires 'manage' on {resource_type} {resource_id}",
    )




@router.get("/organizations/{organization_id}/resource-grants", response_model=List[ResourceGrantSchema])
@requires_permission("view_members")
async def list_resource_grants(
    organization_id: str,
    resource_type: Optional[str] = Query(None),
    resource_id: Optional[str] = Query(None),
    principal_type: Optional[str] = Query(None),
    principal_id: Optional[str] = Query(None),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    return await rbac_service.list_resource_grants(
        db, organization_id, resource_type, resource_id, principal_type, principal_id
    )


@router.post("/organizations/{organization_id}/resource-grants", response_model=ResourceGrantSchema)
async def create_resource_grant(
    organization_id: str,
    data: ResourceGrantCreate,
    request: Request,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    await _require_resource_manage(
        db, current_user, organization_id, data.resource_type, data.resource_id
    )
    grant = await rbac_service.create_resource_grant(db, organization_id, data)
    try:
        await audit_service.log(
            db=db, organization_id=organization_id, action="resource_grant.created",
            user_id=current_user.id, resource_type="resource_grant", resource_id=grant.id,
            details={"resource_type": data.resource_type, "resource_id": data.resource_id,
                     "principal_type": data.principal_type, "principal_id": data.principal_id,
                     "permissions": data.permissions}, request=request,
        )
    except Exception:
        pass
    return grant


async def _load_grant_or_404(db: AsyncSession, org_id: str, grant_id: str) -> ResourceGrant:
    result = await db.execute(
        select(ResourceGrant).where(
            ResourceGrant.id == grant_id,
            ResourceGrant.organization_id == org_id,
            ResourceGrant.deleted_at.is_(None),
        )
    )
    grant = result.scalar_one_or_none()
    if not grant:
        raise HTTPException(status_code=404, detail="Resource grant not found")
    return grant


@router.put("/organizations/{organization_id}/resource-grants/{grant_id}", response_model=ResourceGrantSchema)
async def update_resource_grant(
    organization_id: str,
    grant_id: str,
    data: ResourceGrantUpdate,
    request: Request,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    grant = await _load_grant_or_404(db, organization_id, grant_id)
    await _require_resource_manage(
        db, current_user, organization_id, grant.resource_type, grant.resource_id
    )
    updated = await rbac_service.update_resource_grant(db, organization_id, grant_id, data)
    try:
        await audit_service.log(
            db=db, organization_id=organization_id, action="resource_grant.updated",
            user_id=current_user.id, resource_type="resource_grant", resource_id=grant_id,
            details={"resource_type": grant.resource_type, "resource_id": grant.resource_id,
                     "permissions": data.permissions}, request=request,
        )
    except Exception:
        pass
    return updated


@router.delete("/organizations/{organization_id}/resource-grants/{grant_id}", status_code=204)
async def delete_resource_grant(
    organization_id: str,
    grant_id: str,
    request: Request,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    grant = await _load_grant_or_404(db, organization_id, grant_id)
    await _require_resource_manage(
        db, current_user, organization_id, grant.resource_type, grant.resource_id
    )
    await rbac_service.delete_resource_grant(db, organization_id, grant_id)
    try:
        await audit_service.log(
            db=db, organization_id=organization_id, action="resource_grant.deleted",
            user_id=current_user.id, resource_type="resource_grant", resource_id=grant_id,
            details={"resource_type": grant.resource_type, "resource_id": grant.resource_id},
            request=request,
        )
    except Exception:
        pass
