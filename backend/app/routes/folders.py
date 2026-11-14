from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File as Upload, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user, forbid_service_account_principal
from app.core.permission_resolver import resolve_permissions
from app.dependencies import get_async_db, get_current_organization
from app.ee.audit.service import audit_service
from app.models.eval import TestSuite, TestCase
from app.models.file import File
from app.models.instruction import Instruction
from app.schemas.test_suite_schema import TestCaseCreate
from app.services.folder_service import lock_tree
from app.services.folder_knowledge import RESOURCES, require_capability
from app.models.folder import Folder
from app.models.organization import Organization
from app.models.resource_grant import ResourceGrant
from app.models.user import User
from app.services.folder_service import has_active_manager

router = APIRouter(prefix="/folders", tags=["folders"], dependencies=[Depends(forbid_service_account_principal)])
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]


class FolderCreate(BaseModel):
    parent_id: str | None = None
    name: Name


class FolderPath(BaseModel):
    parent_id: str | None = None
    path_segments: list[Name] = Field(min_length=1, max_length=64)


def can_create_root(rights):
    return (
        rights.has_org_permission("create_data_source")
        or rights.has_org_permission("manage_connections")
        or rights.has_any_resource_permission("manage", "data_source")
        or rights.has_any_resource_permission("create_data_sources", "connection")
    )


def folder_item(folder, rights):
    parent = folder.parent_id
    return {
        "id": folder.id,
        "name": folder.name,
        "parent_id": parent if parent and rights.has_resource_permission("folder", parent, "browse") else None,
        "limited_access": not folder.inherits_access,
        "can_query": rights.has_resource_permission("folder", folder.id, "view"),
        "capabilities": [
            p
            for p in ("manage_instructions", "manage_files", "manage_evals")
            if rights.has_resource_permission("folder", folder.id, p)
        ],
        "can_manage": rights.has_resource_permission("folder", folder.id, "manage"),
        "can_move_from_folder": rights.has_resource_permission("folder", folder.id, "manage")
        and (not parent or rights.has_resource_permission("folder", parent, "manage")),
    }


@router.get("")
async def list_folders(
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
    org: Organization = Depends(get_current_organization),
):
    rights = await resolve_permissions(db, user.id, org.id)
    rows = await db.scalars(select(Folder).where(Folder.organization_id == org.id).order_by(Folder.name))
    return {
        "can_create": can_create_root(rights),
        "folders": [
            folder_item(row, rights) for row in rows if rights.has_resource_permission("folder", row.id, "browse")
        ],
    }


async def write_path(db, user, org, parent_id, segments, reuse):
    await lock_tree(db, org, user)
    if any(n in (".", "..") or "/" in n or "\\" in n for n in segments):
        raise HTTPException(422, "Invalid folder name")
    rights = await resolve_permissions(db, user.id, org.id)
    manager = can_create_root(rights)
    if parent_id is not None:
        parent = await db.scalar(select(Folder.id).where(Folder.id == parent_id, Folder.organization_id == org.id))
        if parent is None or not rights.has_resource_permission("folder", parent_id, "view"):
            raise HTTPException(403, "Folder unavailable")
        manager = rights.has_resource_permission("folder", parent_id, "manage")
    for name in segments:
        folder = await db.scalar(
            select(Folder).where(
                Folder.organization_id == org.id,
                Folder.parent_id == parent_id,
                func.lower(Folder.name) == func.lower(name),
            )
        )
        if folder is None:
            if not manager:
                raise HTTPException(403, "Folder unavailable")
            folder = Folder(organization_id=org.id, parent_id=parent_id, name=name, creator_id=user.id)
            try:
                async with db.begin_nested():
                    db.add(folder)
                    await db.flush()
            except IntegrityError:
                folder = await db.scalar(
                    select(Folder).where(
                        Folder.organization_id == org.id,
                        Folder.parent_id == parent_id,
                        func.lower(Folder.name) == func.lower(name),
                    )
                )
                if folder is None:
                    raise
            else:
                if parent_id is None:
                    db.add(
                        ResourceGrant(
                            organization_id=org.id,
                            resource_type="folder",
                            resource_id=folder.id,
                            principal_type="user",
                            principal_id=user.id,
                            permissions=["manage"],
                        )
                    )
                await audit_service.log(
                    db,
                    org.id,
                    "folder.created",
                    user.id,
                    "folder",
                    folder.id,
                    details={"parent_id": parent_id, "name": name},
                    commit=False,
                )
                parent_id = folder.id
                continue
        if not rights.has_resource_permission("folder", folder.id, "view"):
            raise HTTPException(403, "Folder unavailable")
        if not reuse:
            raise HTTPException(409, "Folder already exists")
        manager = rights.has_resource_permission("folder", folder.id, "manage")
        parent_id = folder.id
    result = {"id": parent_id, "name": folder.name}
    await db.commit()
    return result


@router.post("")
async def create_folder(
    body: FolderCreate,
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
    org: Organization = Depends(get_current_organization),
):
    return await write_path(db, user, org, body.parent_id, [body.name], False)


@router.post("/ensure")
async def ensure_folder(
    body: FolderPath,
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
    org: Organization = Depends(get_current_organization),
):
    return await write_path(db, user, org, body.parent_id, body.path_segments, True)


class FolderShare(BaseModel):
    principal_type: Literal["user", "group"]
    principal_id: str
    role: Literal["Viewer", "Manager"] | None


@router.put("/{folder_id}/share")
async def share(
    folder_id: str,
    body: FolderShare,
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
    org: Organization = Depends(get_current_organization),
):
    await lock_tree(db, org, user)
    from app.models.group import Group
    from app.models.membership import Membership
    from app.services.folder_service import require_folder_manager

    folder = await require_folder_manager(db, org.id, user.id, folder_id)
    if body.principal_type == "user":
        target = select(Membership.id).where(
            Membership.user_id == body.principal_id,
            Membership.organization_id == org.id,
            Membership.deleted_at.is_(None),
        )
    else:
        target = select(Group.id).where(
            Group.id == body.principal_id, Group.organization_id == org.id, Group.deleted_at.is_(None)
        )
    if await db.scalar(target) is None:
        raise HTTPException(404, "Recipient unavailable")
    grants = list(
        await db.scalars(
            select(ResourceGrant)
            .where(
                ResourceGrant.resource_type == "folder",
                ResourceGrant.resource_id == folder_id,
                ResourceGrant.organization_id == org.id,
                ResourceGrant.deleted_at.is_(None),
            )
            .with_for_update()
        )
    )
    grant = next(
        (g for g in grants if g.principal_type == body.principal_type and g.principal_id == body.principal_id), None
    )
    removes_manager = grant and "manage" in grant.permissions and body.role != "Manager"
    if (
        removes_manager
        and (not folder.parent_id or not folder.inherits_access)
        and not await has_active_manager(db, org.id, [g for g in grants if g.id != grant.id])
    ):
        raise HTTPException(409, "Keep a manager")
    if body.role:
        if grant is None:
            grant = ResourceGrant(
                organization_id=org.id,
                resource_type="folder",
                resource_id=folder_id,
                principal_id=body.principal_id,
                principal_type=body.principal_type,
            )
            db.add(grant)
        grant.permissions = ["manage" if body.role == "Manager" else "view"]
    elif grant:
        await db.delete(grant)
    await audit_service.log(
        db, org.id, "folder.shared", user.id, "folder", folder_id, details=body.model_dump(), commit=False
    )
    await db.commit()
    return {"role": body.role}


class Inheritance(BaseModel):
    limited_access: bool


@router.put("/{folder_id}/inheritance")
async def inheritance(
    folder_id: str,
    body: Inheritance,
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
    org: Organization = Depends(get_current_organization),
):
    await lock_tree(db, org, user)
    from app.services.folder_service import require_folder_manager

    folder = await require_folder_manager(db, org.id, user.id, folder_id)
    if body.limited_access:
        grants = await db.scalars(
            select(ResourceGrant).where(
                ResourceGrant.resource_type == "folder",
                ResourceGrant.resource_id == folder_id,
                ResourceGrant.organization_id == org.id,
                ResourceGrant.deleted_at.is_(None),
            )
        )
        if not await has_active_manager(db, org.id, grants):
            raise HTTPException(409, "Choose a manager for this folder")
    await audit_service.log(
        db, org.id, "folder.inheritance_changed", user.id, "folder", folder_id, details=body.model_dump(), commit=False
    )
    folder.inherits_access = not body.limited_access
    await db.commit()
    return {"limited_access": body.limited_access}


@router.get("/{folder_id}/audience")
async def audience(
    folder_id: str,
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
    org: Organization = Depends(get_current_organization),
):
    from app.models.group import Group
    from app.models.membership import Membership
    from app.services.folder_service import require_folder_manager

    folder = await require_folder_manager(db, org.id, user.id, folder_id)
    people = await db.execute(
        select(User.id, User.email)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.organization_id == org.id, Membership.deleted_at.is_(None))
    )
    groups = await db.execute(
        select(Group.id, Group.name).where(Group.organization_id == org.id, Group.deleted_at.is_(None))
    )
    from app.models.role import Role

    org_roles = await db.execute(
        select(Role.id, Role.name).where(Role.organization_id == org.id, Role.deleted_at.is_(None))
    )
    choices = [
        {"id": key, "name": name, "type": kind}
        for kind, rows in (("user", people), ("group", groups), ("role", org_roles))
        for key, name in rows
    ]
    rights = await resolve_permissions(db, user.id, org.id)
    node = folder
    while node is not None:
        grants = await db.scalars(
            select(ResourceGrant).where(
                ResourceGrant.organization_id == org.id,
                ResourceGrant.resource_type == "folder",
                ResourceGrant.resource_id == node.id,
                ResourceGrant.deleted_at.is_(None),
            )
        )
        roles = {
            (g.principal_type, g.principal_id): "Manager"
            if "manage" in g.permissions
            else "Viewer"
            if "view" in g.permissions
            else "Custom"
            for g in grants
        }
        for choice in choices:
            role = roles.get((choice["type"], choice["id"]))
            if node.id == folder.id:
                choice["role"] = role
            elif role and role != "Custom" and rights.has_resource_permission("folder", node.id, "manage"):
                choice.setdefault("inherited", []).append({"role": role, "source": node.name, "source_id": node.id})
        if not node.inherits_access or not node.parent_id:
            break
        node = await db.scalar(select(Folder).where(Folder.organization_id == org.id, Folder.id == node.parent_id))
    return [c for c in choices if c["type"] != "role" or c.get("role") or c.get("inherited")]


class Placement(BaseModel):
    parent_id: str | None = None
    preview: bool = False
    audience_version: str | None = None


@router.put("/items/{kind}/{item_id}/placement")
async def move_item(
    kind: Literal["agent", "folder"],
    item_id: str,
    body: Placement,
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
    org: Organization = Depends(get_current_organization),
):
    await lock_tree(db, org, user)
    from app.models.data_source import DataSource
    from app.services.folder_service import require_folder_manager

    model = DataSource if kind == "agent" else Folder
    resource = "data_source" if kind == "agent" else "folder"
    rows = await db.scalars(
        select(Folder).where(Folder.organization_id == org.id).order_by(Folder.id).with_for_update()
    )
    parents = {f.id: f.parent_id for f in rows}
    item = await db.scalar(select(model).where(model.id == item_id, model.organization_id == org.id).with_for_update())
    rights = await resolve_permissions(db, user.id, org.id)
    if item is None or not rights.has_resource_permission(resource, item_id, "manage"):
        raise HTTPException(403, "Item unavailable")
    source = item.folder_id if kind == "agent" else item.parent_id
    for endpoint in {source, body.parent_id} - {None}:
        await require_folder_manager(db, org.id, user.id, endpoint)
    if kind == "folder":
        if body.parent_id is None and not can_create_root(rights):
            raise HTTPException(403, "Root unavailable")
        ancestor = body.parent_id
        while ancestor:
            if ancestor == item_id:
                raise HTTPException(409, "Cannot move a folder into itself")
            ancestor = parents.get(ancestor)
    # Preview the direct/inherited principals at each parent. A limited item
    # carries its own inheritance boundary across the move.
    import hashlib
    import json

    from app.models.group import Group

    boundaries = set(
        await db.scalars(select(Folder.id).where(Folder.organization_id == org.id, Folder.inherits_access.is_(False)))
    )

    async def inherited(parent):
        if not (item.inherits_access if kind == "folder" else item.inherits_folder_access):
            return {}
        branch = []
        while parent:
            branch.append(parent)
            if parent in boundaries:
                break
            parent = parents[parent]
        grants = await db.scalars(
            select(ResourceGrant).where(
                ResourceGrant.resource_type == "folder",
                ResourceGrant.organization_id == org.id,
                ResourceGrant.resource_id.in_(branch),
                ResourceGrant.deleted_at.is_(None),
            )
        )
        roles = {}
        for g in grants:
            if not {"view", "manage"}.intersection(g.permissions):
                continue
            key = (g.principal_type, g.principal_id)
            roles[key] = max(roles.get(key, 0), 2 if "manage" in g.permissions else 1)
        return roles

    old = await inherited(source)
    new = await inherited(body.parent_id)
    changes = []
    for principal in sorted(old.keys() | new.keys()):
        if old.get(principal) == new.get(principal):
            continue
        kind_name, principal_id = principal
        from app.models.role import Role

        label = (
            await db.scalar(select(Role.name).where(Role.id == principal_id))
            if kind_name == "role"
            else await db.scalar(select(Group.name).where(Group.id == principal_id))
            if kind_name == "group"
            else await db.scalar(select(User.email).where(User.id == principal_id))
        )
        roles = {None: None, 1: "Viewer", 2: "Manager"}
        changes.append({"principal": label, "from": roles[old.get(principal)], "to": roles[new.get(principal)]})
    version = hashlib.sha256(
        json.dumps([kind, item_id, source, body.parent_id, sorted(old.items()), sorted(new.items())], sort_keys=True).encode()
    ).hexdigest()
    if body.preview:
        return {"changes": changes, "audience_version": version}
    if body.audience_version is not None and version != body.audience_version:
        raise HTTPException(409, "Sharing changed. Preview the move again.")
    await audit_service.log(
        db,
        org.id,
        "folder.item_moved",
        user.id,
        resource,
        item_id,
        details={"from": source, "to": body.parent_id},
        commit=False,
    )
    if kind == "folder":
        item.parent_id = body.parent_id
    else:
        item.folder_id = body.parent_id
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Destination unavailable")
    return {"id": item_id, "parent_id": body.parent_id}


@router.patch("/{folder_id}")
async def rename_folder(
    folder_id: str,
    body: FolderCreate,
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
    org: Organization = Depends(get_current_organization),
):
    await lock_tree(db, org, user)
    from app.services.folder_service import require_folder_manager

    folder = await require_folder_manager(db, org.id, user.id, folder_id)
    if body.name in (".", "..") or "/" in body.name or "\\" in body.name:
        raise HTTPException(422, "Invalid folder name")
    folder.name = body.name
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Folder name unavailable")
    return {"name": body.name}


@router.put("/agents/{agent_id}/inheritance")
async def agent_inheritance(
    agent_id: str,
    body: Inheritance,
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
    org: Organization = Depends(get_current_organization),
):
    await lock_tree(db, org, user)
    from app.models.data_source import DataSource
    from app.models.membership import Membership

    agent = await db.scalar(select(DataSource).where(DataSource.id == agent_id, DataSource.organization_id == org.id))
    rights = await resolve_permissions(db, user.id, org.id)
    if agent is None or not rights.has_resource_permission("data_source", agent_id, "manage"):
        raise HTTPException(403, "Agent unavailable")
    if body.limited_access and not await db.scalar(
        select(Membership.id).where(
            Membership.organization_id == org.id,
            Membership.user_id == agent.owner_user_id,
            Membership.user_id.is_not(None),
            Membership.deleted_at.is_(None),
        )
    ):
        raise HTTPException(409, "An active agent owner is required")
    await audit_service.log(
        db,
        org.id,
        "folder.inheritance_changed",
        user.id,
        "data_source",
        agent_id,
        details=body.model_dump(),
        commit=False,
    )
    agent.inherits_folder_access = not body.limited_access
    await db.commit()
    return {"limited_access": body.limited_access}


@router.delete("/{folder_id}", status_code=204)
async def delete_folder(
    folder_id: str,
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
    org: Organization = Depends(get_current_organization),
):
    await lock_tree(db, org, user)
    from sqlalchemy import delete

    from app.models.data_source import DataSource
    from app.models.report import Report
    from app.services.folder_service import require_folder_manager

    folder = await require_folder_manager(db, org.id, user.id, folder_id)
    for _, links, key, _ in RESOURCES.values():
        if await db.scalar(select(key).where(links.c.folder_id == folder_id).limit(1)):
            raise HTTPException(409, "Folder is in use")
    for model in (Instruction, File, TestSuite):
        if await db.scalar(select(model.id).where(model.owner_folder_id == folder_id).limit(1)):
            raise HTTPException(409, "Folder owns knowledge")
    if (
        await db.scalar(select(Folder.id).where(Folder.parent_id == folder_id).limit(1))
        or await db.scalar(select(DataSource.id).where(DataSource.folder_id == folder_id).limit(1))
        or await db.scalar(select(Report.id).where(Report.agent_folder_id == folder_id).limit(1))
    ):
        raise HTTPException(409, "Folder is in use")
    await db.execute(
        delete(ResourceGrant).where(
            ResourceGrant.resource_type == "folder",
            ResourceGrant.organization_id == org.id,
            ResourceGrant.resource_id == folder_id,
        )
    )
    await db.delete(folder)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Folder is in use")


KnowledgeKind = Literal["instructions", "files", "evals"]


@router.get("/{folder_id}/knowledge/{kind}")
async def knowledge_items(
    folder_id: str,
    kind: KnowledgeKind,
    candidates: bool = False,
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
    org: Organization = Depends(get_current_organization),
):
    model, links, key, capability = RESOURCES[kind]
    rights = await require_capability(db, org, user, folder_id, "browse")
    manages = rights.has_resource_permission("folder", folder_id, capability)
    if (
        candidates or kind == "evals" or not rights.has_resource_permission("folder", folder_id, "view")
    ) and not manages:
        raise HTTPException(403, "Folder content unavailable")
    attached = select(key).where(links.c.folder_id == folder_id)
    stmt = select(model).where(
        model.organization_id == org.id,
        model.deleted_at.is_(None),
        ~model.id.in_(attached) if candidates else model.id.in_(attached),
    )
    if kind == "instructions":
        stmt = stmt.options(selectinload(Instruction.data_sources)).where(Instruction.kind == "instruction")
    if kind == "files":
        from app.services.file_access_service import visible_files_clause

        stmt = stmt.where(File.source_kind == "upload", await visible_files_clause(db, user, org))
        if candidates and not rights.has_org_permission("full_admin_access"):
            stmt = stmt.where(File.user_id == user.id)
    visible_rule_ids = None
    if kind == "instructions" and not manages:
        from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

        ids = list(await db.scalars(attached))
        builder = InstructionContextBuilder(
            db, org, user, data_source_ids=[], folder_id=folder_id, mode="chat", channel="app"
        )
        visible_rule_ids = {item.id for item in await builder.load_instructions_by_ids(ids, load_mode_filter=None)}
    result = []
    for item in await db.scalars(stmt):
        if visible_rule_ids is not None and item.id not in visible_rule_ids:
            continue
        if candidates or kind == "evals":
            try:
                await _canonical_authority(db, user, org, kind, item)
            except HTTPException as exc:
                if exc.status_code in (403, 404):
                    continue
                raise
        title = getattr(item, "title", None) or getattr(item, "name", None) or getattr(item, "filename", None)
        if kind == "instructions":
            if not manages and item.status != "published":
                continue
            title = title or item.text[:80]
        result.append({"id": item.id, "name": title, "owned": getattr(item, "owner_folder_id", None) == folder_id})
    return sorted(result, key=lambda r: (r["name"] or "").casefold())


async def _canonical_authority(db, user, org, kind, item):
    if kind == "instructions":
        from app.services.instruction_authority import require_instruction_authority

        await require_instruction_authority(db, user, org, item)
    elif kind == "files":
        rights = await resolve_permissions(db, user.id, org.id)
        if item.source_kind != "upload" or (
            item.user_id != user.id and not rights.has_org_permission("full_admin_access")
        ):
            raise HTTPException(403, "File unavailable")
    else:
        from app.routes.test import _require_suite_authority

        if item.data_source_id:
            from app.core.permissions_decorator import check_resource_permissions
            await check_resource_permissions(db, user.id, org.id, "data_source", [item.data_source_id], "manage_evals")
        await _require_suite_authority(db, user, org, item.id)


@router.put("/{folder_id}/knowledge/{kind}/{item_id}")
@router.delete("/{folder_id}/knowledge/{kind}/{item_id}")
async def change_knowledge_link(
    folder_id: str,
    kind: KnowledgeKind,
    item_id: str,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
    org: Organization = Depends(get_current_organization),
):
    await lock_tree(db, org, user)
    model, links, key, capability = RESOURCES[kind]
    await require_capability(db, org, user, folder_id, capability)
    attach = request.method == "PUT"
    stmt = select(model).where(model.id == item_id, model.organization_id == org.id, model.deleted_at.is_(None))
    if kind == "instructions":
        stmt = stmt.options(
            selectinload(Instruction.data_sources),
            selectinload(Instruction.labels),
            selectinload(Instruction.references),
        ).where(Instruction.kind == "instruction")
    item = await db.scalar(stmt)
    if item is None:
        if not attach:
            return {"attached": False}
        raise HTTPException(404, "Item unavailable")
    condition = (links.c.folder_id == folder_id) & (key == item_id)
    if attach:
        await _canonical_authority(db, user, org, kind, item)
    linked = await db.scalar(select(key).where(condition))
    if bool(linked) != attach:
        if attach:
            await db.execute(insert(links).values(folder_id=folder_id, **{key.name: item_id}))
            if kind == "instructions":
                item.scope_mode = "targeted"
        else:
            await db.execute(delete(links).where(condition))
        await audit_service.log(
            db,
            org.id,
            "folder.item_attached" if attach else "folder.item_detached",
            user.id,
            kind,
            item_id,
            details={"folder_id": folder_id},
            commit=False,
        )
        if kind == "instructions":
            from app.services.instruction_version_service import InstructionVersionService

            version = await InstructionVersionService().create_version(db, item, user.id, commit=False)
            from app.services.instruction_service import InstructionService

            service = InstructionService()
            build = await service.build_service.get_or_create_draft_build(db, org.id, source="user", user_id=user.id)
            await service.build_service.add_to_build(db, build.id, item.id, version.id)
            await service._auto_finalize_build(db, build, user, await service._get_user_permissions(db, user, org))
        await db.commit()
    return {"attached": attach}


class FolderInstructionInput(BaseModel):
    title: Name
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    load_mode: Literal["always", "intelligent"] = "always"


@router.post("/{folder_id}/knowledge/instructions")
async def create_folder_instruction(
    folder_id: str,
    body: FolderInstructionInput,
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
    org: Organization = Depends(get_current_organization),
):
    from app.schemas.instruction_schema import InstructionCreate
    from app.services.instruction_service import InstructionService

    await lock_tree(db, org, user)
    await require_capability(db, org, user, folder_id, "manage_instructions")
    created = await InstructionService().create_instruction(
        db, InstructionCreate(**body.model_dump(), status="published"), user, org, folder_id=folder_id
    )
    return {"id": created.id, "name": created.title}


@router.post("/{folder_id}/knowledge/files")
async def upload_folder_file(
    folder_id: str,
    file: UploadFile = Upload(...),
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
    org: Organization = Depends(get_current_organization),
):
    from app.services.file_service import FileService

    await lock_tree(db, org, user)
    await require_capability(db, org, user, folder_id, "manage_files")
    item = await FileService().upload_file(db, file, user, org, folder_id=folder_id)
    return {"id": item.id, "name": item.filename}


class SuiteName(BaseModel):
    name: Name


@router.post("/{folder_id}/knowledge/evals")
async def create_folder_suite(
    folder_id: str,
    body: SuiteName,
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
    org: Organization = Depends(get_current_organization),
):
    await lock_tree(db, org, user)
    await require_capability(db, org, user, folder_id, "manage_evals")
    suite = TestSuite(name=body.name, owner_folder_id=folder_id, organization_id=org.id)
    db.add(suite)
    await db.flush()
    _, links, _, _ = RESOURCES["evals"]
    await db.execute(links.insert().values(folder_id=folder_id, suite_id=suite.id))
    await db.commit()
    return {"id": suite.id, "name": suite.name}


async def linked_suite(db, org, user, folder_id, suite_id):
    await require_capability(db, org, user, folder_id, "manage_evals")
    _, links, key, _ = RESOURCES["evals"]
    suite = await db.scalar(
        select(TestSuite)
        .join(links, key == TestSuite.id)
        .where(
            links.c.folder_id == folder_id,
            TestSuite.id == suite_id,
            TestSuite.organization_id == org.id,
            TestSuite.deleted_at.is_(None),
        )
    )
    if not suite:
        raise HTTPException(404, "Suite unavailable")
    await _canonical_authority(db, user, org, "evals", suite)
    return suite


@router.post("/{folder_id}/knowledge/evals/{suite_id}/cases")
async def create_folder_case(
    folder_id: str,
    suite_id: str,
    body: TestCaseCreate,
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
    org: Organization = Depends(get_current_organization),
):
    from app.routes.test import case_service

    await lock_tree(db, org, user)
    suite = await linked_suite(db, org, user, folder_id, suite_id)
    if suite.owner_folder_id != folder_id or body.data_source_ids_json:
        raise HTTPException(409, "Select the owning folder to author a routing case")
    case = await case_service.create_case(
        db, org.id, user, suite_id, body.name, body.prompt_json, body.expectations_json
    )
    return {"id": case.id, "name": case.name}


@router.post("/{folder_id}/knowledge/evals/{suite_id}/runs")
async def run_folder_suite(
    folder_id: str,
    suite_id: str,
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
    org: Organization = Depends(get_current_organization),
):
    from app.routes.test import run_service

    await linked_suite(db, org, user, folder_id, suite_id)
    cases = list(
        await db.scalars(
            select(TestCase.id).where(
                TestCase.suite_id == suite_id, TestCase.status == "active", TestCase.deleted_at.is_(None)
            )
        )
    )
    run = await run_service.create_run(db, org, user, case_ids=cases, folder_id=folder_id)
    return {"id": run.id, "status": run.status}


@router.get("/{folder_id}/knowledge/evals/{suite_id}/cases")
async def folder_cases(
    folder_id: str,
    suite_id: str,
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
    org: Organization = Depends(get_current_organization),
):
    await linked_suite(db, org, user, folder_id, suite_id)
    cases = await db.scalars(
        select(TestCase)
        .where(TestCase.suite_id == suite_id, TestCase.deleted_at.is_(None))
        .order_by(TestCase.name, TestCase.id)
    )
    return [
        {
            "id": c.id,
            "name": c.name,
            "status": c.status,
            "prompt_json": c.prompt_json,
            "expectations_json": c.expectations_json,
        }
        for c in cases
    ]


@router.patch("/{folder_id}/knowledge/evals/{suite_id}/cases/{case_id}")
async def edit_folder_case(
    folder_id: str,
    suite_id: str,
    case_id: str,
    body: TestCaseCreate,
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
    org: Organization = Depends(get_current_organization),
):
    from app.routes.test import case_service

    await lock_tree(db, org, user)
    suite = await linked_suite(db, org, user, folder_id, suite_id)
    case = await case_service.get_case(db, org.id, user, case_id)
    if (
        suite.owner_folder_id != folder_id
        or case.suite_id != suite.id
        or body.data_source_ids_json
        or case.data_source_ids_json
    ):
        raise HTTPException(404, "Routing case unavailable")
    result = await case_service.update_case(
        db, org.id, user, case_id, body.name, body.prompt_json, body.expectations_json, None
    )
    return {"id": result.id, "name": result.name}
