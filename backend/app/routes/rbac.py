from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional

from app.dependencies import get_async_db, get_current_organization
from app.models.user import User
from app.models.organization import Organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.ee.license import require_enterprise
from app.services.rbac_service import rbac_service
from app.schemas.rbac_schema import (
    RoleCreate, RoleUpdate, RoleSchema,
    GroupCreate, GroupUpdate, GroupSchema, GroupMemberAdd, GroupMemberSchema,
    RoleAssignmentCreate, RoleAssignmentSchema,
    ResourceGrantCreate, ResourceGrantUpdate, ResourceGrantSchema,
)

router = APIRouter(tags=["rbac"])


# ── Permission Registry ──────────────────────────────────────────────────

@router.get("/permissions/registry")
async def get_permissions_registry():
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
@requires_permission("view_organization_members")
async def list_roles(
    organization_id: str,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    return await rbac_service.list_roles(db, organization_id)


@router.post("/organizations/{organization_id}/roles", response_model=RoleSchema)
@require_enterprise(feature="custom_roles")
@requires_permission("manage_roles")
async def create_role(
    organization_id: str,
    data: RoleCreate,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    return await rbac_service.create_role(db, organization_id, data)


@router.put("/organizations/{organization_id}/roles/{role_id}", response_model=RoleSchema)
@require_enterprise(feature="custom_roles")
@requires_permission("manage_roles")
async def update_role(
    organization_id: str,
    role_id: str,
    data: RoleUpdate,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    return await rbac_service.update_role(db, organization_id, role_id, data)


@router.delete("/organizations/{organization_id}/roles/{role_id}", status_code=204)
@require_enterprise(feature="custom_roles")
@requires_permission("manage_roles")
async def delete_role(
    organization_id: str,
    role_id: str,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    await rbac_service.delete_role(db, organization_id, role_id)


# ── Groups ───────────────────────────────────────────────────────────────

@router.get("/organizations/{organization_id}/groups", response_model=List[GroupSchema])
@require_enterprise(feature="custom_roles")
@requires_permission("manage_groups")
async def list_groups(
    organization_id: str,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    return await rbac_service.list_groups(db, organization_id)


@router.post("/organizations/{organization_id}/groups", response_model=GroupSchema)
@require_enterprise(feature="custom_roles")
@requires_permission("manage_groups")
async def create_group(
    organization_id: str,
    data: GroupCreate,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    return await rbac_service.create_group(db, organization_id, data)


@router.put("/organizations/{organization_id}/groups/{group_id}", response_model=GroupSchema)
@require_enterprise(feature="custom_roles")
@requires_permission("manage_groups")
async def update_group(
    organization_id: str,
    group_id: str,
    data: GroupUpdate,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    return await rbac_service.update_group(db, organization_id, group_id, data)


@router.delete("/organizations/{organization_id}/groups/{group_id}", status_code=204)
@require_enterprise(feature="custom_roles")
@requires_permission("manage_groups")
async def delete_group(
    organization_id: str,
    group_id: str,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    await rbac_service.delete_group(db, organization_id, group_id)


@router.get("/organizations/{organization_id}/groups/{group_id}/members", response_model=List[GroupMemberSchema])
@require_enterprise(feature="custom_roles")
@requires_permission("manage_groups")
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
@requires_permission("manage_groups")
async def add_group_member(
    organization_id: str,
    group_id: str,
    data: GroupMemberAdd,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    await rbac_service.add_group_member(db, organization_id, group_id, data.user_id)


@router.delete("/organizations/{organization_id}/groups/{group_id}/members/{user_id}", status_code=204)
@require_enterprise(feature="custom_roles")
@requires_permission("manage_groups")
async def remove_group_member(
    organization_id: str,
    group_id: str,
    user_id: str,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    await rbac_service.remove_group_member(db, organization_id, group_id, user_id)


# ── Role Assignments ─────────────────────────────────────────────────────

@router.get("/organizations/{organization_id}/role-assignments", response_model=List[RoleAssignmentSchema])
@requires_permission("view_organization_members")
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
@requires_permission("manage_role_assignments")
async def create_role_assignment(
    organization_id: str,
    data: RoleAssignmentCreate,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    return await rbac_service.create_role_assignment(db, organization_id, data)


@router.delete("/organizations/{organization_id}/role-assignments/{assignment_id}", status_code=204)
@requires_permission("manage_role_assignments")
async def delete_role_assignment(
    organization_id: str,
    assignment_id: str,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    await rbac_service.delete_role_assignment(db, organization_id, assignment_id)


# ── Resource Grants ──────────────────────────────────────────────────────

@router.get("/organizations/{organization_id}/resource-grants", response_model=List[ResourceGrantSchema])
@requires_permission("view_organization_members")
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
@requires_permission("manage_resource_grants")
async def create_resource_grant(
    organization_id: str,
    data: ResourceGrantCreate,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    return await rbac_service.create_resource_grant(db, organization_id, data)


@router.put("/organizations/{organization_id}/resource-grants/{grant_id}", response_model=ResourceGrantSchema)
@requires_permission("manage_resource_grants")
async def update_resource_grant(
    organization_id: str,
    grant_id: str,
    data: ResourceGrantUpdate,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    return await rbac_service.update_resource_grant(db, organization_id, grant_id, data)


@router.delete("/organizations/{organization_id}/resource-grants/{grant_id}", status_code=204)
@requires_permission("manage_resource_grants")
async def delete_resource_grant(
    organization_id: str,
    grant_id: str,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    await rbac_service.delete_resource_grant(db, organization_id, grant_id)
