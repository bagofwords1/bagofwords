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
    role_ids = [g.principal_id for g in managers if g.principal_type == "role"]
    if role_ids:
        from app.models.role_assignment import RoleAssignment
        assignments = await db.execute(select(RoleAssignment.principal_type, RoleAssignment.principal_id).where(
            RoleAssignment.organization_id == org_id, RoleAssignment.role_id.in_(role_ids),
            RoleAssignment.deleted_at.is_(None)))
        for kind, principal in assignments:
            if kind == "user":
                user_ids.append(principal)
            elif kind == "group":
                group_ids.append(principal)
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


async def load_report_folders(db, report):
    """Load scope when a lightweight report query suppressed relationship loading."""
    from sqlalchemy import inspect
    from app.models.report import report_folder_ids
    state = inspect(report, raiseerr=False)
    if getattr(report, "agent_folder_id", None) and state is not None and "additional_folders" in state.unloaded:
        await db.refresh(report, ["additional_folders"])
    return report_folder_ids(report)


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
    from app.models.report import folder_report_knowledge as history

    selected = set(await load_report_folders(db, report))
    recorded = set(await db.scalars(select(history.c.folder_id).where(history.c.report_id == report.id)))
    folders = selected | recorded
    live = set(await db.scalars(select(Folder.id).where(
        Folder.id.in_(folders), Folder.organization_id == org.id, Folder.deleted_at.is_(None),
    ))) if folders else set()
    archived = folders - live
    if archived and report.user_id != user.id and not rights.has_org_permission("full_admin_access"):
        return False
    for fid in selected & live:
        curator = (not required and report.user_id == user.id and
                   await training_curator_only(db, org.id, user.id, fid, report.mode))
        if not curator and not rights.has_resource_permission("folder", fid, "view"):
            return False
    from app.services.folder_knowledge import can_read_knowledge_scope
    if not await can_read_knowledge_scope(db, report, org, user, rights, archived=archived):
        return False
    if required:
        visible = await visible_data_source_ids(db, user, org)
        return required <= visible
    return True


async def queryable_folder_tree(db, organization, user):
    """Return only queryable nodes, with hidden parents clipped."""
    if not user or not organization:
        return {}
    from app.core.permission_resolver import resolve_permissions
    rights = await resolve_permissions(db, user.id, organization.id)
    rows = await db.execute(select(Folder.id, Folder.parent_id, Folder.name).where(
        Folder.organization_id == organization.id, Folder.deleted_at.is_(None)))
    visible = {fid: (parent, name) for fid, parent, name in rows
               if rights.has_resource_permission("folder", fid, "view")}
    return {fid: (parent if parent in visible else None, name) for fid, (parent, name) in visible.items()}


def folder_path(tree, folder_id, separator=" / "):
    names = []
    seen = set()
    while folder_id in tree and folder_id not in seen:
        seen.add(folder_id)
        folder_id, name = tree[folder_id]
        names.append(name)
    return separator.join(reversed(names))


async def visible_agent_paths(db, organization, user, agents):
    homes = {str(a.id): a.folder_id for a in agents if getattr(a, "folder_id", None)}
    if not homes:
        return {}
    tree = await queryable_folder_tree(db, organization, user)
    paths = {fid: folder_path(tree, fid) for fid in set(homes.values()) if fid in tree}
    return {aid: paths[home] for aid, home in homes.items() if home in paths}



async def lock_tree(db, org, user):
    """Serialize grant and structural writes, including SQLite's first write."""
    from app.core.permission_resolver import invalidate_rbac_memo
    from app.models.organization import Organization
    from sqlalchemy import update

    await db.execute(update(Organization).where(Organization.id == org.id).values(updated_at=Organization.updated_at))
    invalidate_rbac_memo(db, user.id, org.id)


async def validate_scope_folders(db, org_id, user_id, ids):
    if not ids:
        return []
    from app.core.permission_resolver import resolve_permissions
    wanted = list(dict.fromkeys(ids))
    rights = await resolve_permissions(db, user_id, org_id)
    existing = set(await db.scalars(select(Folder.id).where(
        Folder.organization_id == org_id, Folder.deleted_at.is_(None), Folder.id.in_(wanted))))
    if any(fid not in existing or not rights.has_resource_permission("folder", fid, "view") for fid in wanted):
        raise HTTPException(403, "Folder unavailable")
    return wanted


async def allowed_folder_agents(db, org_id, ids):
    """Expand an application/channel ceiling. Limited-access subtrees require an explicit selection."""
    if not ids:
        return set()
    from app.models.data_source import DataSource
    rows = (await db.execute(select(Folder.id, Folder.parent_id, Folder.inherits_access).where(
        Folder.organization_id == org_id, Folder.deleted_at.is_(None)))).all()
    children = {}
    for fid, parent, inherits in rows:
        if inherits:
            children.setdefault(parent, []).append(fid)
    pending = list(set(ids).intersection(fid for fid, _, _ in rows))
    branch = set()
    while pending:
        fid = pending.pop()
        if fid not in branch:
            branch.add(fid)
            pending.extend(children.get(fid, []))
    return set(await db.scalars(select(DataSource.id).where(
        DataSource.organization_id == org_id, DataSource.deleted_at.is_(None),
        DataSource.inherits_folder_access.is_(True), DataSource.folder_id.in_(branch))))


def can_create_root(rights):
    return (
        rights.has_org_permission("create_data_source")
        or rights.has_org_permission("manage_connections")
        or rights.has_any_resource_permission("manage", "data_source")
        or rights.has_any_resource_permission("create_data_sources", "connection")
    )


async def browsable_folder_tree(db, organization, user):
    """Folders this user may see by name: ones they can query (``view``) and
    ones they curate (``manage_instructions`` without view, so a curator can
    target a folder's rules). Hidden parents are clipped, so a folder deep in
    a private branch shows up as a root and never reveals the branch's names.

    Returns ``({folder_id: (clipped_parent_id, name)}, rights)``.
    """
    from app.core.permission_resolver import resolve_permissions
    if not user or not organization:
        return {}, None
    rights = await resolve_permissions(db, user.id, organization.id)
    rows = await db.execute(select(Folder.id, Folder.parent_id, Folder.name).where(
        Folder.organization_id == organization.id, Folder.deleted_at.is_(None)))
    shown = {
        fid: (parent, name) for fid, parent, name in rows
        if rights.has_resource_permission("folder", fid, "view")
        or rights.has_resource_permission("folder", fid, "manage_instructions")
    }
    return {fid: (parent if parent in shown else None, name) for fid, (parent, name) in shown.items()}, rights
