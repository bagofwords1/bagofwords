"""Resolve the facts an RLS policy binds to, for one user in one org.

Four identity surfaces already exist and none of them needed inventing:

| Surface | Where it comes from |
|---|---|
| OIDC / Entra groups | `groups` + `group_memberships`, synced from token claims |
| Entra profile attributes | `Membership.profile_attributes`, synced on login from Graph `/me` |
| Roles | `roles` + `role_assignments` (principal is a user **or** a group) |
| Org membership | `Membership.role`, `User.email` |

`profile_attributes` is the most useful of the four: already per-user and
per-org, admin-curated (the org picks which attributes sync), and already
rendered into `<user_profile>`, so an admin can see the values that exist
before writing a rule against them.

Resolution is one call per request, deliberately not cached across users. The
precedent to avoid is `org-row-limit-ignored-on-refresh.md`, where five
services built executors without `organization_settings` and the row cap was
silently skipped on every rerun path. The identical bug here leaks rows instead
of truncating them.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_sources.fast.rls import ANONYMOUS, Identity

logger = logging.getLogger(__name__)


async def resolve_identity(
    db: AsyncSession, user, organization_id: str
) -> Identity:
    """Build the `Identity` a policy compiles against.

    A missing user is ANONYMOUS, not a bypass — with default-deny that means no
    rows from any RLS-protected relation. Background paths that legitimately
    need data (the scheduled refresh) do not read through RLS at all; they
    extract with the connection's own credential.

    Failures degrade to a *narrower* identity, never a wider one: a groups
    lookup that errors leaves the group set empty, which denies rather than
    grants.
    """
    if user is None or not getattr(user, "id", None):
        return ANONYMOUS

    from app.models.group import Group
    from app.models.group_membership import GroupMembership
    from app.models.membership import Membership
    from app.models.role import Role
    from app.models.role_assignment import RoleAssignment

    user_id = str(user.id)
    identity = Identity(user_id=user_id, email=getattr(user, "email", None))

    # --- Entra / IdP profile attributes ------------------------------------
    try:
        membership = (await db.execute(
            select(Membership).where(
                Membership.user_id == user_id,
                Membership.organization_id == str(organization_id),
            )
        )).scalars().first()
        if membership is not None and isinstance(membership.profile_attributes, dict):
            identity.profile_attributes = dict(membership.profile_attributes)
    except Exception:
        logger.warning("rls.identity.profile_lookup_failed", exc_info=True)

    # --- groups -------------------------------------------------------------
    try:
        rows = (await db.execute(
            select(Group.id, Group.name)
            .join(GroupMembership, GroupMembership.group_id == Group.id)
            .where(
                GroupMembership.user_id == user_id,
                Group.organization_id == str(organization_id),
            )
        )).all()
        identity.group_ids = {str(gid) for gid, _ in rows}
        identity.group_names = {str(name) for _, name in rows if name}
    except Exception:
        logger.warning("rls.identity.group_lookup_failed", exc_info=True)

    # --- roles --------------------------------------------------------------
    # Assigned directly to the user, or inherited through a group. Inherited
    # roles are the common case in an org that manages access through the IdP,
    # so leaving them out would make group-based policies quietly useless.
    try:
        principals = [("user", user_id)] + [("group", gid) for gid in identity.group_ids]
        conditions = [
            (RoleAssignment.principal_type == ptype) & (RoleAssignment.principal_id == pid)
            for ptype, pid in principals
        ]
        clause = conditions[0]
        for c in conditions[1:]:
            clause = clause | c
        rows = (await db.execute(
            select(Role.id, Role.name)
            .join(RoleAssignment, RoleAssignment.role_id == Role.id)
            .where(
                RoleAssignment.organization_id == str(organization_id),
                clause,
            )
        )).all()
        identity.role_ids = {str(rid) for rid, _ in rows}
        identity.role_names = {str(name) for _, name in rows if name}
    except Exception:
        logger.warning("rls.identity.role_lookup_failed", exc_info=True)

    return identity


async def available_attribute_keys(db: AsyncSession, organization_id: str) -> list[str]:
    """Profile attribute keys the org actually syncs, for the policy editor.

    Offering the real keys (rather than a hardcoded Entra list) is what stops an
    admin binding a policy to an attribute that will never resolve — which, with
    default-deny, is an empty relation and a support ticket.
    """
    from app.models.membership import Membership

    keys: set[str] = set()
    try:
        rows = (await db.execute(
            select(Membership.profile_attributes).where(
                Membership.organization_id == str(organization_id),
                Membership.profile_attributes.isnot(None),
            )
        )).scalars().all()
        for attrs in rows:
            if isinstance(attrs, dict):
                keys.update(str(k) for k in attrs.keys())
    except Exception:
        logger.warning("rls.identity.attribute_keys_failed", exc_info=True)
    return sorted(keys)
