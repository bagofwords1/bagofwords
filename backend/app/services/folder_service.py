from fastapi import HTTPException
from sqlalchemy import select

from app.models.data_source import DataSource
from app.models.folder import Folder


async def expand_folder_permissions(db, org_id, permissions):
    direct = {rid: rights for (kind, rid), rights in permissions.items() if kind == "folder"}
    if not direct:
        return
    rows = (
        await db.execute(
            select(Folder.id, Folder.parent_id, Folder.inherits_access).where(
                Folder.organization_id == org_id, Folder.deleted_at.is_(None)
            )
        )
    ).all()
    tree = {row.id: row.parent_id if row.inherits_access else None for row in rows}
    effective = {}
    for folder_id in tree:
        path = []
        seen = set()
        node = folder_id
        while node in tree and node not in effective:
            if node in seen:
                raise ValueError("Folder cycle")
            seen.add(node)
            path.append(node)
            node = tree[node]
        inherited = effective.get(node, set())
        for child in reversed(path):
            inherited = inherited | (direct.get(child, set()) & {"view", "manage"})
            if "manage" in inherited:
                inherited = inherited | {"view"}
            effective[child] = inherited
            local = inherited | direct.get(child, set())
            if "manage" in local:
                local = local | {"manage_instructions", "manage_files", "manage_evals"}
            if local:
                permissions["folder", child] = local | {"browse"}
    visible = [fid for fid, rights in effective.items() if "view" in rights]
    if visible:
        agents = await db.execute(
            select(DataSource.id, DataSource.folder_id).where(
                DataSource.organization_id == org_id,
                DataSource.folder_id.in_(visible),
                DataSource.inherits_folder_access.is_(True),
                DataSource.deleted_at.is_(None),
            )
        )
        for agent_id, folder_id in agents:
            rights = permissions.setdefault(("data_source", agent_id), set())
            rights.add("query")
            if "manage" in effective[folder_id]:
                rights.add("manage")


async def require_folder_manager(db, org_id, user_id, folder_id):
    from app.core.permission_resolver import resolve_permissions

    folder = await db.scalar(
        select(Folder).where(Folder.organization_id == org_id, Folder.id == folder_id, Folder.deleted_at.is_(None))
    )
    resolved = await resolve_permissions(db, str(user_id), str(org_id))
    if folder is None or not resolved.has_resource_permission("folder", folder_id, "manage"):
        raise HTTPException(403, "Folder unavailable")
    return folder


async def visible_branch(db, org_id, user_id, folder_id):
    from app.core.permission_resolver import resolve_permissions

    rights = await resolve_permissions(db, str(user_id), str(org_id))
    rows = (
        await db.execute(
            select(Folder.id, Folder.parent_id).where(Folder.organization_id == org_id, Folder.deleted_at.is_(None))
        )
    ).all()
    children = {}
    visible = set()
    for fid, parent in rows:
        if rights.has_resource_permission("folder", fid, "view"):
            children.setdefault(parent, []).append(fid)
            visible.add(fid)
    if folder_id not in visible:
        raise HTTPException(403, "Folder unavailable")
    branch = set()
    pending = [folder_id]
    while pending:
        node = pending.pop()
        if node not in branch:
            branch.add(node)
            pending.extend(children.get(node, ()))
    return branch


async def training_curator_only(db, org_id, user_id, folder_id, mode):
    if mode != "training" or not folder_id or not user_id:
        return False
    from app.core.permission_resolver import resolve_permissions
    rights = await resolve_permissions(db, user_id, org_id)
    return (rights.has_resource_permission("folder", folder_id, "manage_instructions")
            and not rights.has_resource_permission("folder", folder_id, "view")
            and bool(await db.scalar(select(Folder.id).where(Folder.id == folder_id,
                Folder.organization_id == org_id, Folder.deleted_at.is_(None)))))


async def require_report_folder(db, org, user, folder_id, mode):
    if not await training_curator_only(db, org.id, user.id, folder_id, mode):
        await visible_branch(db, org.id, user.id, folder_id)


async def has_active_manager(db, org_id, grants):
    from app.models.group_membership import GroupMembership
    from app.models.membership import Membership
    from app.models.user import User

    managers = [g for g in grants if "manage" in g.permissions]
    user_ids = [g.principal_id for g in managers if g.principal_type == "user"]
    group_ids = [g.principal_id for g in managers if g.principal_type == "group"]
    members = select(GroupMembership.user_id).where(
        GroupMembership.group_id.in_(group_ids), GroupMembership.deleted_at.is_(None)
    )
    query = (
        select(User.id)
        .join(Membership, Membership.user_id == User.id)
        .where(
            Membership.organization_id == org_id,
            Membership.deleted_at.is_(None),
            User.is_active.is_(True),
            User.id.in_(user_ids) | User.id.in_(members),
        )
    )
    return await db.scalar(query.limit(1)) is not None


async def record_output_scope(db, report_id, agent_ids):
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from app.models.report import folder_report_sources

    if agent_ids:
        insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
        await db.execute(
            insert(folder_report_sources)
            .values([{"report_id": report_id, "agent_id": agent_id} for agent_id in sorted(set(agent_ids))])
            .on_conflict_do_nothing()
        )
        await db.flush()


async def can_read_folder_output(db, report, user):
    from app.core.derived_access import visible_data_source_ids
    from app.models.organization import Organization
    from app.models.report import folder_report_sources as sources

    required = set(await db.scalars(select(sources.c.agent_id).where(sources.c.report_id == report.id)))
    if not required and not report.agent_folder_id:
        from app.models.report import folder_report_knowledge
        if not await db.scalar(select(folder_report_knowledge.c.report_id).where(
                folder_report_knowledge.c.report_id == report.id).limit(1)):
            return True
    if not user:
        return False
    org = await db.get(Organization, report.organization_id)
    from app.core.permission_resolver import resolve_permissions
    rights = await resolve_permissions(db, user.id, org.id)
    from app.models.report import report_folder_ids
    for fid in report_folder_ids(report):
        curator = (not required and report.user_id == user.id and
                   await training_curator_only(db, org.id, user.id, fid, report.mode))
        if not curator and not rights.has_resource_permission("folder", fid, "view"):
            return False
    from app.services.folder_knowledge import can_read_knowledge_scope
    if not await can_read_knowledge_scope(db, report, org, user, rights):
        return False
    if required:
        visible = await visible_data_source_ids(db, user, org)
        return required <= visible
    return True


async def visible_agent_paths(db, organization, user, agents):
    """Visible suffix of each home path. Stop at the first invisible ancestor."""
    homes = {str(a.id): a.folder_id for a in agents if getattr(a, "folder_id", None)}
    if not homes or not user or not organization:
        return {}
    from app.core.permission_resolver import resolve_permissions
    rights = await resolve_permissions(db, user.id, organization.id)
    rows = await db.execute(select(Folder.id, Folder.parent_id, Folder.name).where(
        Folder.organization_id == organization.id, Folder.deleted_at.is_(None)))
    visible = {fid: (parent, name) for fid, parent, name in rows
               if rights.has_resource_permission("folder", fid, "view")}
    paths = {}
    for home in set(homes.values()):
        names, seen = [], set()
        node = home
        while node in visible and node not in seen:
            seen.add(node)
            node, name = visible[node]
            names.append(name)
        if names:
            paths[home] = " / ".join(reversed(names))
    return {aid: paths[home] for aid, home in homes.items() if home in paths}


async def lock_tree(db, org, user):
    """Serialize grant and structural writes, including SQLite's first write."""
    from app.core.permission_resolver import invalidate_rbac_memo
    from app.models.organization import Organization
    from sqlalchemy import update

    await db.execute(update(Organization).where(Organization.id == org.id).values(updated_at=Organization.updated_at))
    invalidate_rbac_memo(db, user.id, org.id)
