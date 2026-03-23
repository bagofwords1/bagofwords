"""
Centralized RBAC permission resolver.

Resolves a user's effective permissions (org-level and resource-level)
by unioning all roles assigned directly or via groups.

The resolver is cached per-request on request.state to avoid repeated queries.
"""
import logging
from dataclasses import dataclass, field
from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException

from app.models.role import Role
from app.models.role_assignment import RoleAssignment
from app.models.resource_grant import ResourceGrant
from app.models.group_membership import GroupMembership
from app.models.membership import Membership, ROLES_PERMISSIONS

logger = logging.getLogger(__name__)

FULL_ADMIN = "full_admin_access"


@dataclass
class ResolvedPermissions:
    """Resolved effective permissions for a user within an organization."""

    org_permissions: set = field(default_factory=set)
    resource_permissions: dict = field(default_factory=dict)  # (resource_type, resource_id) -> set[str]
    role_names: list = field(default_factory=list)

    def has_org_permission(self, permission: str) -> bool:
        """Check if user has an org-level permission. full_admin_access bypasses."""
        return FULL_ADMIN in self.org_permissions or permission in self.org_permissions

    def has_resource_permission(self, resource_type: str, resource_id: str, permission: str) -> bool:
        """Check if user has a specific resource-level permission. full_admin_access bypasses."""
        if FULL_ADMIN in self.org_permissions:
            return True
        key = (resource_type, resource_id)
        return permission in self.resource_permissions.get(key, set())

    def has_resource_membership(self, resource_type: str, resource_id: str) -> bool:
        """Binary check — is user a member of this resource at all? (non-enterprise path)"""
        if FULL_ADMIN in self.org_permissions:
            return True
        key = (resource_type, resource_id)
        return key in self.resource_permissions


async def resolve_permissions(
    db: AsyncSession, user_id: str, org_id: str
) -> ResolvedPermissions:
    """
    Resolve effective permissions for a user in an organization.

    1. Find user's groups
    2. Find all roles assigned to user or their groups in this org
    3. Union all role permissions → org_permissions
    4. Find all resource grants for user or their groups → resource_permissions
    5. Fallback to old Membership.role if no role_assignments exist (dual-read)
    """
    try:
        return await _resolve_permissions_inner(db, user_id, org_id)
    except Exception:
        logger.error(
            "Permission resolution failed for user=%s org=%s",
            user_id, org_id, exc_info=True,
        )
        # Audit the failure
        try:
            from app.ee.audit.service import audit_service
            await audit_service.log(
                db=db,
                organization_id=org_id,
                action="rbac.resolution_failed",
                user_id=user_id,
                resource_type="permission",
                details={"error": "Permission resolution failed"},
            )
        except Exception:
            logger.debug("Failed to audit permission resolution failure", exc_info=True)
        # Return empty permissions on failure — caller will deny access
        return ResolvedPermissions()


async def _resolve_permissions_inner(
    db: AsyncSession, user_id: str, org_id: str
) -> ResolvedPermissions:
    """Inner implementation of permission resolution."""
    # 1. Get user's group IDs in this org
    group_stmt = (
        select(GroupMembership.group_id)
        .where(GroupMembership.user_id == user_id)
    )
    group_result = await db.execute(group_stmt)
    group_ids = [row[0] for row in group_result.all()]

    # 2. Build principal matching condition (user directly OR via groups)
    principal_conditions = [
        and_(
            RoleAssignment.principal_type == "user",
            RoleAssignment.principal_id == user_id,
        )
    ]
    if group_ids:
        principal_conditions.append(
            and_(
                RoleAssignment.principal_type == "group",
                RoleAssignment.principal_id.in_(group_ids),
            )
        )

    # 3. Fetch role assignments with joined role data
    role_stmt = (
        select(Role.name, Role.permissions)
        .join(RoleAssignment, RoleAssignment.role_id == Role.id)
        .where(
            or_(*principal_conditions),
            # Match org-specific roles OR system roles (org_id IS NULL)
            or_(
                RoleAssignment.organization_id == org_id,
                Role.organization_id.is_(None),
            ),
            RoleAssignment.organization_id == org_id,
            RoleAssignment.deleted_at.is_(None),
            Role.deleted_at.is_(None),
        )
    )
    role_result = await db.execute(role_stmt)
    role_rows = role_result.all()

    # Union all permissions from all assigned roles
    org_permissions = set()
    role_names = []
    for role_name, permissions_list in role_rows:
        role_names.append(role_name)
        if isinstance(permissions_list, list):
            org_permissions.update(permissions_list)

    # 4. Fetch resource grants
    grant_principal_conditions = [
        and_(
            ResourceGrant.principal_type == "user",
            ResourceGrant.principal_id == user_id,
        )
    ]
    if group_ids:
        grant_principal_conditions.append(
            and_(
                ResourceGrant.principal_type == "group",
                ResourceGrant.principal_id.in_(group_ids),
            )
        )

    grant_stmt = (
        select(
            ResourceGrant.resource_type,
            ResourceGrant.resource_id,
            ResourceGrant.permissions,
        )
        .where(
            or_(*grant_principal_conditions),
            ResourceGrant.organization_id == org_id,
            ResourceGrant.deleted_at.is_(None),
        )
    )
    grant_result = await db.execute(grant_stmt)
    grant_rows = grant_result.all()

    resource_permissions = {}
    for resource_type, resource_id, perms in grant_rows:
        key = (resource_type, resource_id)
        if key not in resource_permissions:
            resource_permissions[key] = set()
        if isinstance(perms, list):
            resource_permissions[key].update(perms)

    # 5. Dual-read fallback: if no role_assignments exist, use old Membership.role
    if not role_rows:
        membership_stmt = (
            select(Membership.role)
            .where(
                Membership.user_id == user_id,
                Membership.organization_id == org_id,
                Membership.deleted_at.is_(None),
            )
        )
        membership_result = await db.execute(membership_stmt)
        membership_row = membership_result.scalar_one_or_none()
        if membership_row:
            role_names = [membership_row]
            org_permissions = set(ROLES_PERMISSIONS.get(membership_row, set()))
            logger.debug(
                "RBAC fallback: using Membership.role=%s for user=%s org=%s",
                membership_row, user_id, org_id,
            )

    return ResolvedPermissions(
        org_permissions=org_permissions,
        resource_permissions=resource_permissions,
        role_names=role_names,
    )


async def get_resolved_permissions(request, db: AsyncSession, user, organization) -> ResolvedPermissions:
    """
    Request-scoped cached resolver. Call this from decorators/routes
    to avoid re-querying permissions multiple times per request.
    """
    cache_key = f"rbac_{user.id}_{organization.id}"
    if hasattr(request, 'state') and hasattr(request.state, cache_key):
        return getattr(request.state, cache_key)

    resolved = await resolve_permissions(db, str(user.id), str(organization.id))

    if hasattr(request, 'state'):
        setattr(request.state, cache_key, resolved)

    return resolved


async def assert_full_admin_exists(
    db: AsyncSession,
    org_id: str,
    exclude_user_id: str = None,
    exclude_role_id: str = None,
) -> None:
    """
    Ensure at least one direct user (not group) holds full_admin_access
    after the proposed change.

    Groups are excluded because their membership can be emptied externally
    (IdP sync, SCIM). Only direct user assignments count for lockout prevention.

    Args:
        db: Database session
        org_id: Organization ID
        exclude_user_id: User being removed (count without them)
        exclude_role_id: Role being edited/deleted (count without it)
    """
    # Find all roles that contain "full_admin_access" in their permissions
    all_roles_stmt = (
        select(Role.id, Role.permissions)
        .where(
            Role.deleted_at.is_(None),
            or_(
                Role.organization_id == org_id,
                Role.organization_id.is_(None),
            ),
        )
    )
    all_roles_result = await db.execute(all_roles_stmt)
    admin_role_ids = []
    for role_id, perms in all_roles_result.all():
        if role_id == exclude_role_id:
            continue
        if isinstance(perms, list) and FULL_ADMIN in perms:
            admin_role_ids.append(role_id)

    if not admin_role_ids:
        raise HTTPException(
            status_code=409,
            detail="At least one user must have full admin access",
        )

    # Count distinct direct users assigned to any of these roles
    from sqlalchemy import func

    count_stmt = (
        select(func.count(func.distinct(RoleAssignment.principal_id)))
        .where(
            RoleAssignment.organization_id == org_id,
            RoleAssignment.principal_type == "user",
            RoleAssignment.role_id.in_(admin_role_ids),
            RoleAssignment.deleted_at.is_(None),
        )
    )
    if exclude_user_id:
        count_stmt = count_stmt.where(
            RoleAssignment.principal_id != exclude_user_id
        )

    result = await db.execute(count_stmt)
    count = result.scalar()

    if count == 0:
        raise HTTPException(
            status_code=409,
            detail="At least one user must have full admin access",
        )
