"""Who reaches an agent WITHOUT a grant on the agent itself.

The agent's members list is its direct grants. The permission resolver also
hands out agent access from two other places, and this module names the
principals behind them so the access panel can show the whole audience:

* a ``view``/``manage`` grant on the agent's folder or an ancestor it inherits
  from (``folder_service.expand_folder_permissions``);
* a ``manage_data_sources`` grant on every connection the agent uses
  (``permission_resolver._agents_fully_backed_by_connections``).

Org-wide authority (full admin) is not a per-principal grant and is not listed.
"""
from sqlalchemy import select

from app.core.permission_resolver import resolve_permissions
from app.models.data_source import DataSource
from app.models.folder import Folder
from app.models.resource_grant import ResourceGrant


async def _grants(db, org_id, resource_type, resource_ids):
    if not resource_ids:
        return []
    return list(await db.scalars(select(ResourceGrant).where(
        ResourceGrant.organization_id == org_id,
        ResourceGrant.resource_type == resource_type,
        ResourceGrant.resource_id.in_(resource_ids),
        ResourceGrant.deleted_at.is_(None),
    )))


async def _principal_names(db, org_id, keys):
    """Resolve (type, id) -> (name, email). Principals that no longer exist are omitted."""
    from app.models.group import Group
    from app.models.membership import Membership
    from app.models.role import Role
    from app.models.user import User

    ids = {kind: [pid for k, pid in keys if k == kind] for kind in ("user", "group", "role")}
    names = {}
    if ids["user"]:
        rows = await db.execute(
            select(User.id, User.name, User.email)
            .join(Membership, Membership.user_id == User.id)
            .where(User.id.in_(ids["user"]), Membership.organization_id == org_id,
                   Membership.deleted_at.is_(None)))
        for uid, name, email in rows:
            names["user", uid] = (name or email, email)
    if ids["group"]:
        rows = await db.execute(select(Group.id, Group.name).where(
            Group.id.in_(ids["group"]), Group.organization_id == org_id, Group.deleted_at.is_(None)))
        for gid, name in rows:
            names["group", gid] = (name, None)
    if ids["role"]:
        rows = await db.execute(select(Role.id, Role.name).where(
            Role.id.in_(ids["role"]), Role.deleted_at.is_(None)))
        for rid, name in rows:
            names["role", rid] = (name, None)
    return names


async def inherited_agent_access(db, organization, user, data_source_id):
    """Return ``{"principals": [...], "hidden": bool}`` for one agent.

    ``hidden`` is true when some inherited access exists that this viewer may
    not enumerate: folder grants are only named to a manager of that folder
    (the same rule as the folder's own audience), connection grants only to
    someone who can manage the agent's members.
    """
    from app.models.connection import Connection
    from app.models.domain_connection import domain_connection

    org_id = str(organization.id)
    agent = await db.scalar(select(DataSource).where(
        DataSource.id == data_source_id, DataSource.organization_id == org_id,
        DataSource.deleted_at.is_(None)))
    if agent is None:
        return {"principals": [], "hidden": False}
    rights = await resolve_permissions(db, str(user.id), org_id)

    found = []  # (principal_type, principal_id, access, source_type, source_id, source_name)
    hidden = False

    # Folder chain: the agent's folder, then each parent while the child inherits.
    if agent.folder_id and agent.inherits_folder_access:
        folder_id, seen = agent.folder_id, set()
        while folder_id and folder_id not in seen:
            seen.add(folder_id)
            folder = await db.scalar(select(Folder).where(
                Folder.id == folder_id, Folder.organization_id == org_id, Folder.deleted_at.is_(None)))
            if folder is None:
                break
            grants = [g for g in await _grants(db, org_id, "folder", [folder.id])
                      if {"view", "manage"} & set(g.permissions or [])]
            if grants and not rights.has_resource_permission("folder", folder.id, "manage"):
                hidden = True
            else:
                for g in grants:
                    access = "manage" if "manage" in g.permissions else "query"
                    found.append((g.principal_type, g.principal_id, access, "folder", folder.id, folder.name))
            folder_id = folder.parent_id if folder.inherits_access else None

    # Connections: a principal manages the agent when it holds
    # `manage_data_sources` on ALL of the agent's connections. (A user who
    # covers them only by combining several principals is not attributable to
    # one row and is not listed.)
    connections = (await db.execute(
        select(Connection.id, Connection.name)
        .join(domain_connection, domain_connection.c.connection_id == Connection.id)
        .where(domain_connection.c.data_source_id == agent.id)
    )).all()
    if connections:
        covered = {}
        for g in await _grants(db, org_id, "connection", [c.id for c in connections]):
            if "manage_data_sources" in (g.permissions or []):
                covered.setdefault((g.principal_type, g.principal_id), set()).add(g.resource_id)
        full = [key for key, ids in covered.items() if ids == {c.id for c in connections}]
        if full and not rights.has_resource_permission("data_source", agent.id, "manage_members"):
            hidden = True
        else:
            label = ", ".join(sorted(c.name for c in connections))
            for kind, pid in full:
                found.append((kind, pid, "manage", "connection", connections[0].id, label))

    names = await _principal_names(db, org_id, {(k, p) for k, p, *_ in found})
    principals = [
        {
            "principal_type": kind, "principal_id": pid,
            "principal_name": names[kind, pid][0], "principal_email": names[kind, pid][1],
            "access": access, "source_type": source_type, "source_id": source_id, "source_name": source_name,
        }
        for kind, pid, access, source_type, source_id, source_name in found
        # The owner always manages their agent and is already shown as such.
        if (kind, pid) in names and (kind, pid) != ("user", agent.owner_user_id)
    ]
    principals.sort(key=lambda p: ((p["principal_name"] or "").lower(), p["source_type"], p["source_name"]))
    return {"principals": principals, "hidden": hidden}
