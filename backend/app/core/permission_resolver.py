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
    # Org connection-admin: manage every connection's config AND create agents
    # on any connection. It is deliberately NOT `manage_data_sources` — managing
    # *other people's* agents stays an explicit, opt-in per-connection grant.
    "manage_connections":  {"connection": {"manage_connection", "create_data_sources"}},
}

# A `manage` grant on a data source is the agent-owner/manager tier: it is a
# superset that implies the specific management permissions enforced across the
# agent's surfaces (instructions, entities, evals, membership). This is what
# lets a non-admin who creates/owns an agent fully manage *that* agent without
# extra per-permission grants, while still scoping them to their own agents —
# unlike the org-level `manage_*` perms which apply to every data source.
RESOURCE_PERM_IMPLIES: dict[str, dict[str, set[str]]] = {
    "data_source": {
        "manage": {
            "manage_instructions",
            "create_entities",
            "manage_evals",
            "manage_members",
            "view",
            "view_schema",
        },
    },
    "connection": {
        # Managing all agents on a connection includes being able to create them.
        "manage_data_sources": {"create_data_sources"},
    },
}


def _grant_implies(resource_type: str, granted: set, permission: str) -> bool:
    """True if any held resource grant implies `permission` (e.g. `manage`)."""
    by_perm = RESOURCE_PERM_IMPLIES.get(resource_type, {})
    for held in granted:
        if permission in by_perm.get(held, set()):
            return True
    return False


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
        org-perm implications (ORG_PERM_IMPLIES_RESOURCE) → grant implications
        (RESOURCE_PERM_IMPLIES, e.g. `manage` ⇒ manage_instructions) →
        explicit grant.
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
        granted = self.resource_permissions.get(key, set())
        # Implied by a superset grant on this resource (e.g. `manage`).
        if _grant_implies(resource_type, granted, permission):
            return True
        # Explicit grant
        return permission in granted

    def has_any_resource_permission(self, permission: str, resource_type: str | None = None) -> bool:
        """True if the user holds `permission` on at least one resource, via an
        explicit grant or a superset grant (e.g. `manage`).

        Used by resource-scoped route gating as a cheap pre-filter before the
        specific resource_id is checked in the route body — it must honour the
        same grant implications as ``has_resource_permission`` so an agent
        manager (holding only `manage`) isn't rejected at the door.
        """
        if FULL_ADMIN in self.org_permissions:
            return True
        for (rtype, _rid), perms in self.resource_permissions.items():
            if resource_type is not None and rtype != resource_type:
                continue
            if permission in perms or _grant_implies(rtype, perms, permission):
                return True
        return False

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


async def assert_principal_belongs_to_org(db: AsyncSession, user, org_id: str) -> None:
    """Raise 403 unless ``user`` is bound to ``org_id`` (member or service account).

    The single membership invariant: whenever a user id and an org id both
    exist for a request/job, the principal must still be bound to that org.
    Enforced at every non-bootstrap entry point (HTTP org dependency, MCP auth,
    completion creation, scheduled-prompt execution, external-platform messages)
    so a removed member can't keep acting in the org via any surface that
    bypasses ``@requires_permission``.
    """
    if not await principal_belongs_to_org(db, user, org_id):
        raise HTTPException(status_code=403, detail="User is not a member of this organization")


async def ensure_system_role_assignment(
    db: AsyncSession, org_id: str, user_id: str, role_name: str,
) -> None:
    """Idempotently ensure ``user_id`` holds the system role ``role_name`` in
    ``org_id`` (RBAC path). Does NOT commit — the caller commits.

    Used by provisioning paths (SCIM/LDAP/OIDC) that create a ``Membership`` so
    the user gets a real ``role_assignment`` instead of relying on the resolver's
    transitional legacy-role net.
    """
    from app.models.role_assignment import RoleAssignment

    role = (await db.execute(
        select(Role).where(
            Role.name == role_name,
            Role.is_system == True,
            Role.organization_id.is_(None),
            Role.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if not role:
        return
    existing = (await db.execute(
        select(RoleAssignment).where(
            RoleAssignment.organization_id == org_id,
            RoleAssignment.role_id == role.id,
            RoleAssignment.principal_type == "user",
            RoleAssignment.principal_id == user_id,
            RoleAssignment.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if existing:
        return
    db.add(RoleAssignment(
        organization_id=org_id,
        role_id=role.id,
        principal_type="user",
        principal_id=user_id,
    ))


async def resolve_permissions(
    db: AsyncSession, user_id: str, org_id: str
) -> ResolvedPermissions:
    """
    Resolve effective permissions for a user in an organization.

    1. Find user's groups
    2. Find all roles assigned to user or their groups in this org
    3. Union all role permissions → org_permissions
    4. Find all resource grants for user or their groups → resource_permissions
    5. Transitional net: if NO role_assignments exist, fall back to the baseline
       permissions implied by the legacy Membership.role (dual-read). This is a
       temporary bridge for un-backfilled/SSO-provisioned users and is removed
       once every membership is guaranteed a role_assignment.
    """
    memo = _rbac_memo(db)
    if memo is not None and (user_id, org_id) in memo:
        return memo[(user_id, org_id)]
    try:
        resolved = await _resolve_permissions_inner(db, user_id, org_id)
        if memo is not None:
            memo[(user_id, org_id)] = resolved
        return resolved
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

    # Transitional backward-compat net: a membership that resolves to NO RBAC
    # role assignment (e.g. a user provisioned via SCIM/LDAP/OIDC before the
    # assignment backfill ran) falls back to the baseline permissions implied by
    # its legacy ``Membership.role`` string. This guarantees such a user is never
    # stranded with zero permissions during the RBAC transition. It only fires
    # when there are NO assignments at all, so it never masks a real RBAC role
    # (e.g. a user explicitly demoted to member keeps exactly the member set).
    # Remove once every membership is guaranteed a role_assignment.
    if not role_rows:
        from app.models.membership import Membership
        from app.core.permissions_registry import DEFAULT_MEMBER_PERMISSIONS

        legacy_role = (await db.execute(
            select(Membership.role).where(
                Membership.user_id == user_id,
                Membership.organization_id == org_id,
                Membership.deleted_at.is_(None),
            )
        )).scalars().first()
        if legacy_role == "admin":
            org_permissions.add(FULL_ADMIN)
            role_names.append("admin")
        elif legacy_role == "member":
            org_permissions.update(DEFAULT_MEMBER_PERMISSIONS)
            role_names.append("member")

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

    # Connection `manage_data_sources` grant ⇒ `manage` on every agent fully
    # backed by those connections. Expanded here (rather than at check time) so
    # the per-agent `manage` superset (instructions/entities/evals) and list
    # visibility both work uniformly. Only EXPLICIT per-connection grants
    # cascade — org `manage_connections` is connection-admin, not agent-admin.
    managed_conn_ids = [
        rid for (rtype, rid), perms in resource_permissions.items()
        if rtype == "connection" and "manage_data_sources" in perms
    ]
    for ds_id in await _agents_fully_backed_by_connections(db, managed_conn_ids):
        resource_permissions.setdefault(("data_source", ds_id), set()).add("manage")

    return ResolvedPermissions(
        org_permissions=org_permissions,
        resource_permissions=resource_permissions,
        role_names=role_names,
    )


async def _agents_fully_backed_by_connections(
    db: AsyncSession, connection_ids: list[str],
) -> set[str]:
    """Return data_source ids whose connections are ALL within ``connection_ids``.

    ALL-connections semantics: an agent that draws on connections the caller
    cannot fully manage is excluded, since it exposes data from every
    connection it uses. Agents with no connections are excluded.
    """
    if not connection_ids:
        return set()
    from app.models.domain_connection import domain_connection

    granted = set(connection_ids)
    # Candidate agents: linked to at least one granted connection.
    cand = await db.execute(
        select(domain_connection.c.data_source_id)
        .where(domain_connection.c.connection_id.in_(connection_ids))
        .distinct()
    )
    candidate_ids = [r[0] for r in cand.all()]
    if not candidate_ids:
        return set()
    # Pull every connection of those candidates; keep only fully-granted ones.
    rows = await db.execute(
        select(domain_connection.c.data_source_id, domain_connection.c.connection_id)
        .where(domain_connection.c.data_source_id.in_(candidate_ids))
    )
    conns_by_ds: dict[str, set] = {}
    for ds_id, conn_id in rows.all():
        conns_by_ds.setdefault(ds_id, set()).add(conn_id)
    return {ds_id for ds_id, conns in conns_by_ds.items() if conns and conns <= granted}


async def resolve_permissions_bulk(
    db: AsyncSession, user_id: str, org_ids: list[str]
) -> dict[str, ResolvedPermissions]:
    """Resolve permissions for a user across MANY orgs in a constant number of
    queries (instead of ``resolve_permissions`` once per org).

    whoami loops every org the user belongs to; calling ``resolve_permissions``
    per org is ~3 queries × N orgs (serialized round-trips). This collapses the
    group / role / grant lookups into three org-spanning queries and reconstructs
    each org's ``ResolvedPermissions`` in Python, mirroring
    ``_resolve_permissions_inner`` exactly. Returns a dict keyed by org id (every
    requested org is present, empty perms if nothing matched).
    """
    result: dict[str, ResolvedPermissions] = {oid: ResolvedPermissions() for oid in org_ids}
    if not org_ids:
        return result
    try:
        # 1. Group memberships across all requested orgs (1 query).
        group_rows = (await db.execute(
            select(Group.organization_id, GroupMembership.group_id)
            .join(Group, Group.id == GroupMembership.group_id)
            .where(GroupMembership.user_id == user_id,
                   Group.organization_id.in_(org_ids))
        )).all()
        groups_by_org: dict[str, list] = {}
        all_group_ids: list = []
        for org_id, group_id in group_rows:
            groups_by_org.setdefault(org_id, []).append(group_id)
            all_group_ids.append(group_id)

        # 2. Role assignments + role data (1 query). Mirrors the per-org filter:
        #    RoleAssignment.organization_id == org AND principal is the user or one
        #    of the user's groups. Bucketed by org in Python.
        role_principal = [and_(RoleAssignment.principal_type == "user",
                               RoleAssignment.principal_id == user_id)]
        if all_group_ids:
            role_principal.append(and_(RoleAssignment.principal_type == "group",
                                       RoleAssignment.principal_id.in_(all_group_ids)))
        role_rows = (await db.execute(
            select(RoleAssignment.organization_id, RoleAssignment.principal_type,
                   RoleAssignment.principal_id, Role.id, Role.name, Role.permissions)
            .join(RoleAssignment, RoleAssignment.role_id == Role.id)
            .where(or_(*role_principal),
                   RoleAssignment.organization_id.in_(org_ids),
                   RoleAssignment.deleted_at.is_(None),
                   Role.deleted_at.is_(None))
        )).all()
        org_perms: dict[str, set] = {oid: set() for oid in org_ids}
        role_names_by_org: dict[str, list] = {oid: [] for oid in org_ids}
        role_ids_by_org: dict[str, list] = {oid: [] for oid in org_ids}
        for org_id, p_type, p_id, role_id, role_name, perms in role_rows:
            if org_id not in org_perms:
                continue
            # Per-org principal check (a group id belongs to exactly one org).
            if p_type == "group" and p_id not in groups_by_org.get(org_id, ()):
                continue
            role_ids_by_org[org_id].append(role_id)
            role_names_by_org[org_id].append(role_name)
            if isinstance(perms, list):
                org_perms[org_id].update(perms)
        all_role_ids = [rid for ids in role_ids_by_org.values() for rid in ids]

        # 3. Resource grants (1 query) for user / groups / roles across all orgs.
        grant_principal = [and_(ResourceGrant.principal_type == "user",
                                ResourceGrant.principal_id == user_id)]
        if all_group_ids:
            grant_principal.append(and_(ResourceGrant.principal_type == "group",
                                        ResourceGrant.principal_id.in_(all_group_ids)))
        if all_role_ids:
            grant_principal.append(and_(ResourceGrant.principal_type == "role",
                                        ResourceGrant.principal_id.in_(all_role_ids)))
        grant_rows = (await db.execute(
            select(ResourceGrant.organization_id, ResourceGrant.principal_type,
                   ResourceGrant.principal_id, ResourceGrant.resource_type,
                   ResourceGrant.resource_id, ResourceGrant.permissions)
            .where(or_(*grant_principal),
                   ResourceGrant.organization_id.in_(org_ids),
                   ResourceGrant.deleted_at.is_(None))
        )).all()
        res_perms_by_org: dict[str, dict] = {oid: {} for oid in org_ids}
        for org_id, p_type, p_id, r_type, r_id, perms in grant_rows:
            if org_id not in res_perms_by_org:
                continue
            if p_type == "group" and p_id not in groups_by_org.get(org_id, ()):
                continue
            if p_type == "role" and p_id not in role_ids_by_org.get(org_id, ()):
                continue
            key = (r_type, r_id)
            bucket = res_perms_by_org[org_id]
            if key not in bucket:
                bucket[key] = set()
            if isinstance(perms, list):
                bucket[key].update(perms)

        # 4. Expand connection manage_data_sources → per-agent manage. Rare (only
        #    when the user holds explicit per-connection grants); ~free otherwise
        #    (early return on empty). Kept per-org for exact parity.
        for org_id in org_ids:
            res_perms = res_perms_by_org[org_id]
            managed_conn_ids = [rid for (rtype, rid), perms in res_perms.items()
                                if rtype == "connection" and "manage_data_sources" in perms]
            for ds_id in await _agents_fully_backed_by_connections(db, managed_conn_ids):
                res_perms.setdefault(("data_source", ds_id), set()).add("manage")
            result[org_id] = ResolvedPermissions(
                org_permissions=org_perms[org_id],
                resource_permissions=res_perms,
                role_names=role_names_by_org[org_id],
            )
        # Warm the per-request memo so later single-org lookups are free.
        memo = _rbac_memo(db)
        if memo is not None:
            for org_id, resolved in result.items():
                memo[(user_id, org_id)] = resolved
        return result
    except Exception:
        logger.error("Bulk permission resolution failed for user=%s", user_id, exc_info=True)
        # Fall back to per-org resolution so a batch bug never denies access.
        for org_id in org_ids:
            result[org_id] = await resolve_permissions(db, user_id, org_id)
        return result


def _rbac_memo(db: AsyncSession):
    """Per-request (per-session) memo dict for resolved permissions, or None.

    Stored on the session's ``.info`` mapping, which lives for exactly one
    request (the session is created per request via ``get_async_db``). This makes
    repeated ``resolve_permissions`` calls for the same (user, org) within one
    request free — several list endpoints resolve permissions 2-4× per request.
    """
    try:
        return db.info.setdefault("_rbac_memo", {})
    except Exception:
        return None


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


def llm_access_control_active() -> bool:
    """Whether per-model LLM access control is enforced.

    This is an enterprise feature. When the license does not include it, the
    enforcement path fails OPEN — every model behaves as unrestricted, exactly
    like the community build. This keeps a billing lapse from locking an org
    out of its own models.
    """
    try:
        from app.ee.license import has_feature
        return has_feature("llm_access_control")
    except Exception:
        return False


async def get_accessible_model_ids(
    db: AsyncSession, user_id: str, org_id: str,
) -> tuple[bool, list[str]]:
    """Returns (is_admin, model_ids_the_user_holds_a `use` grant for).

    - is_admin=True means the user has full_admin_access; callers should not
      filter (every model is accessible).
    - model_ids are LLMModel ids granted directly, via group, or via role.
      Unrestricted models and org defaults are NOT included here — callers
      handle those via ``user_can_use_model`` / the restriction flag.
    """
    resolved = await resolve_permissions(db, str(user_id), str(org_id))
    if FULL_ADMIN in resolved.org_permissions:
        return True, []
    return False, [
        rid for (rtype, rid), perms in resolved.resource_permissions.items()
        if rtype == "llm_model" and "use" in perms
    ]


async def user_can_use_model(
    db: AsyncSession, user_id: str, org_id: str, model,
) -> bool:
    """Capability check for a single LLM model.

    Order: feature off (fail open) → unrestricted → org default/small-default
    (always available) → full admin → explicit `use` grant.
    """
    if not llm_access_control_active():
        return True
    if not getattr(model, "is_restricted", False):
        return True
    # Org default + small default are always available to every member (D3).
    if getattr(model, "is_default", False) or getattr(model, "is_small_default", False):
        return True
    is_admin, granted = await get_accessible_model_ids(db, user_id, org_id)
    return is_admin or str(model.id) in set(granted)


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


async def get_user_ids_with_permission(
    db: AsyncSession, org_id: str, permission: str, data_source_id: str | None = None,
) -> list[str]:
    """Inverse of ``get_ds_ids_with_permission``: the user ids in an org who hold
    ``permission`` — full admins always, plus (when ``data_source_id`` is given)
    anyone with that permission on that specific agent/data source.

    ``data_source_id=None`` => full admins only (a "global" item's audience).

    Implemented by enumerating org members and reusing the forward resolver, so
    it stays consistent with per-request permission checks. O(members) — fine for
    the notification fan-out fired on discrete events; revisit with a set-based
    query if it ever runs on a hot path.
    """
    from app.models.membership import Membership

    rows = (await db.execute(
        select(Membership.user_id).where(and_(
            Membership.organization_id == str(org_id),
            Membership.user_id.isnot(None),
            Membership.deleted_at.is_(None),
        ))
    )).all()
    out: list[str] = []
    seen: set[str] = set()
    target = str(data_source_id) if data_source_id is not None else None
    for (uid,) in rows:
        uid = str(uid)
        if uid in seen:
            continue
        is_admin, ds_ids = await get_ds_ids_with_permission(db, uid, str(org_id), permission)
        if is_admin or (target is not None and target in set(ds_ids)):
            out.append(uid)
            seen.add(uid)
    return out


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
