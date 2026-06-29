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
from app.models.group import Group
from app.models.group_membership import GroupMembership

logger = logging.getLogger(__name__)

FULL_ADMIN = "full_admin_access"

# Org-level permissions that implicitly grant specific per-resource permissions.
# E.g. holding `manage_instructions` at the org level means the user can
# create/edit instructions on any data source, without needing per-DS grants.
# Likewise, `manage_connections` (org-level connection admin) implies the
# ability to create data sources/agents on any connection, so a connection
# admin doesn't need a per-connection `manage_data_sources` grant.
ORG_PERM_IMPLIES_RESOURCE: dict[str, dict[str, set[str]]] = {
    "manage_instructions": {"data_source": {"manage_instructions"}},
    "manage_entities":     {"data_source": {"create_entities"}},
    "manage_evals":        {"data_source": {"manage_evals"}},
    "manage_connections":  {"connection": {"manage_data_sources"}},
}


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
        """Check if user has a specific resource-level permission.

        Tiers: full_admin → implicit view/view_schema (any grant) →
        org-perm implications (ORG_PERM_IMPLIES_RESOURCE) → explicit grant.
        """
        if FULL_ADMIN in self.org_permissions:
            return True
        key = (resource_type, resource_id)
        # `view` and `view_schema` are implicit: any grant on the resource
        # implies the holder can see the resource and its schema. They are
        # no longer surfaced as explicit checkbox permissions.
        if resource_type == "data_source" and permission in ("view", "view_schema"):
            if key in self.resource_permissions:
                return True
        # Implied by an org-level admin permission
        for org_perm in self.org_permissions:
            implied = ORG_PERM_IMPLIES_RESOURCE.get(org_perm, {}).get(resource_type)
            if implied and permission in implied:
                return True
        # Explicit grant
        return permission in self.resource_permissions.get(key, set())

    def has_resource_membership(self, resource_type: str, resource_id: str) -> bool:
        """Binary check — is user a member of this resource at all? (non-enterprise path)"""
        if FULL_ADMIN in self.org_permissions:
            return True
        key = (resource_type, resource_id)
        return key in self.resource_permissions


async def principal_belongs_to_org(db: AsyncSession, user, org_id: str) -> bool:
    """Whether a principal is bound to an organization.

    Humans are bound via a ``Membership`` row. Service accounts have no
    ``Membership`` (so they consume no seat and never appear in member lists);
    they are bound via a ``ServiceAccount`` row whose ``organization_id``
    matches and which is not disabled/deleted.
    """
    from app.models.membership import Membership

    if getattr(user, "is_service_account", False):
        from app.models.service_account import ServiceAccount
        result = await db.execute(
            select(ServiceAccount).where(
                ServiceAccount.user_id == str(user.id),
                ServiceAccount.organization_id == str(org_id),
                ServiceAccount.disabled_at.is_(None),
                ServiceAccount.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none() is not None

    result = await db.execute(
        select(Membership).where(
            Membership.user_id == str(user.id),
            Membership.organization_id == str(org_id),
        )
    )
    return result.scalar_one_or_none() is not None


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
        .join(Group, Group.id == GroupMembership.group_id)
        .where(
            GroupMembership.user_id == user_id,
            Group.organization_id == org_id,
        )
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
        select(Role.id, Role.name, Role.permissions)
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
    role_ids = []
    for role_id, role_name, permissions_list in role_rows:
        role_ids.append(role_id)
        role_names.append(role_name)
        if isinstance(permissions_list, list):
            org_permissions.update(permissions_list)

    # 4. Fetch resource grants (user, groups, and roles the user has)
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
    if role_ids:
        grant_principal_conditions.append(
            and_(
                ResourceGrant.principal_type == "role",
                ResourceGrant.principal_id.in_(role_ids),
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

    return ResolvedPermissions(
        org_permissions=org_permissions,
        resource_permissions=resource_permissions,
        role_names=role_names,
    )


async def get_accessible_data_source_ids(
    db: AsyncSession, user_id: str, org_id: str,
) -> tuple:
    """
    Returns (is_admin, accessible_ds_ids).

    - is_admin=True means the user has full_admin_access; callers should not filter.
    - accessible_ds_ids is a list of data_source ids the user can see via:
      legacy DataSourceMembership (user) OR ResourceGrant (user/group/role).
      Public data sources are NOT included here — callers must OR them in.

    Use this for capability checks ("can this user access DS X if they
    navigate to it?"). For default list views, prefer
    ``get_member_data_source_ids`` so admins only see DSs they are
    explicitly members of.
    """
    resolved = await resolve_permissions(db, str(user_id), str(org_id))
    is_admin = FULL_ADMIN in resolved.org_permissions
    if is_admin:
        return True, []
    return False, await _resolved_member_ds_ids(db, user_id, resolved)


async def get_member_data_source_ids(
    db: AsyncSession, user_id: str, org_id: str,
) -> list[str]:
    """Return data_source IDs where the user holds an explicit grant or
    legacy membership (direct, via group, or via role).

    Unlike ``get_accessible_data_source_ids``, this does NOT short-circuit
    on ``full_admin_access``. Admins get the same explicit-only view as any
    other user — they only see DSs they actually joined or created. They
    can still navigate directly to any DS via their admin bypass at the
    capability layer (``get_accessible_data_source_ids``,
    ``user_can_access_data_source``).

    Public data sources are NOT included; callers must OR them in.
    """
    resolved = await resolve_permissions(db, str(user_id), str(org_id))
    return await _resolved_member_ds_ids(db, user_id, resolved)


async def can_view_all_data_sources(
    db: AsyncSession, user_id: str, org_id: str,
) -> bool:
    """Org-wide data-source governance capability.

    True for full admins (``full_admin_access``) and connection admins
    (``manage_connections``) — the principals responsible for data sources
    across the whole org. This gates the admin "show all" view on the data
    sources list.

    Deliberately does NOT consider per-data-source ``manage`` grants: that
    permission is scoped to a single data source and confers no authority to
    discover or browse other users' private data sources. A per-DS admin
    already sees the data sources they manage in their normal list via their
    explicit grant.
    """
    resolved = await resolve_permissions(db, str(user_id), str(org_id))
    return (
        FULL_ADMIN in resolved.org_permissions
        or resolved.has_org_permission("manage_connections")
    )


async def _resolved_member_ds_ids(
    db: AsyncSession, user_id: str, resolved: ResolvedPermissions,
) -> list[str]:
    from app.models.data_source_membership import DataSourceMembership, PRINCIPAL_TYPE_USER
    ds_ids = {
        rid for (rtype, rid) in resolved.resource_permissions.keys()
        if rtype == "data_source"
    }
    mem_result = await db.execute(
        select(DataSourceMembership.data_source_id).where(
            DataSourceMembership.principal_type == PRINCIPAL_TYPE_USER,
            DataSourceMembership.principal_id == str(user_id),
        )
    )
    for (ds_id,) in mem_result.all():
        ds_ids.add(ds_id)
    return list(ds_ids)


async def get_ds_ids_with_permission(
    db: AsyncSession, user_id: str, org_id: str, permission: str
) -> tuple[bool, list[str]]:
    """Returns (is_full_admin, ds_ids_where_user_holds_the_given_permission).

    is_full_admin=True means the caller should skip all DS-level filtering.
    """
    resolved = await resolve_permissions(db, str(user_id), str(org_id))
    if resolved.has_org_permission(permission):
        return True, []
    matching = [
        rid for (rtype, rid), perms in resolved.resource_permissions.items()
        if rtype == "data_source" and permission in perms
    ]
    return False, matching


async def user_can_access_data_source(
    db: AsyncSession, user_id: str, org_id: str, ds, ds_id: str = None,
) -> bool:
    """Check if a user can access a single data source (public bypass + grants/memberships)."""
    if ds is not None and getattr(ds, 'is_public', False):
        return True
    is_admin, accessible = await get_accessible_data_source_ids(db, user_id, org_id)
    if is_admin:
        return True
    target = ds_id if ds_id is not None else (str(ds.id) if ds is not None else None)
    return target in set(accessible)


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
