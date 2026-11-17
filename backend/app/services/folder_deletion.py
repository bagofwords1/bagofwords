"""Apply a folder deletion as one transaction, including agent cleanup."""

from fastapi import HTTPException
from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permission_resolver import resolve_permissions
from app.models.data_source import DataSource
from app.models.data_source_file_association import data_source_file_association
from app.models.eval import TestCase
from app.models.folder import Folder
from app.models.instruction import instruction_data_source_association
from app.models.project import project_file_association
from app.models.report_file_association import report_file_association
from app.models.resource_grant import ResourceGrant
from app.services.folder_knowledge import RESOURCES
from app.services.folder_service import has_active_manager, lock_tree, require_folder_manager


async def delete_tree(db, org, actor, folder_id, mode, destination_id=None):
    # Agent and knowledge services commit between steps. The connection owns
    # the transaction, so those commits cannot leave a partially removed tree.
    from app.models.organization import Organization
    from app.models.user import User
    from app.services.file_service import discard_file_bytes

    async with (
        db.bind.begin() as connection,
        AsyncSession(bind=connection, expire_on_commit=False, join_transaction_mode="rollback_only") as batch,
    ):
        organization = await batch.get(Organization, org.id)
        user = await batch.get(User, actor.id)
        paths = await _delete_tree(batch, organization, user, folder_id, mode, destination_id)
        await batch.flush()
    await discard_file_bytes(paths)


async def _delete_tree(db, org, user, folder_id, mode, destination_id):
    from app.ee.audit.service import audit_service
    from app.services.data_source_service import DataSourceService

    await lock_tree(db, org, user)
    root = await require_folder_manager(db, org.id, user.id, folder_id)
    rights = await resolve_permissions(db, user.id, org.id)

    def require(kind, ids, permission="manage"):
        if any(not rights.has_resource_permission(kind, key, permission) for key in ids):
            raise HTTPException(403, "You need manage access to every affected item.")

    folders = list(await db.scalars(select(Folder).where(Folder.organization_id == org.id)))
    children = {}
    for folder in folders:
        children.setdefault(folder.parent_id, []).append(folder.id)
    removing = [root.id]
    if mode == "contents":
        for fid in removing:
            removing.extend(children.get(fid, []))
    require("folder", removing)
    direct = children.get(root.id, [])
    agents = list(
        await db.scalars(
            select(DataSource.id).where(DataSource.organization_id == org.id, DataSource.folder_id.in_(removing))
        )
    )
    require("data_source", agents)
    if mode == "keep":
        require("folder", direct)
        destination_id = destination_id or root.parent_id
        if destination_id:
            await require_folder_manager(db, org.id, user.id, destination_id)
            descendants = [root.id]
            for fid in descendants:
                descendants.extend(children.get(fid, []))
            if destination_id in descendants:
                raise HTTPException(409, "Choose a folder outside this branch.")
        elif direct:
            from app.services.folder_service import can_create_root

            if not can_create_root(rights):
                raise HTTPException(403, "Root unavailable")
            for fid in direct:
                grants = await db.scalars(
                    select(ResourceGrant).where(
                        ResourceGrant.organization_id == org.id,
                        ResourceGrant.resource_type == "folder",
                        ResourceGrant.resource_id == fid,
                        ResourceGrant.deleted_at.is_(None),
                    )
                )
                if not await has_active_manager(db, org.id, grants):
                    raise HTTPException(
                        409, "Choose a destination folder or assign a direct manager to each child folder."
                    )
        await db.execute(update(Folder).where(Folder.id.in_(direct)).values(parent_id=destination_id))
        await db.execute(update(DataSource).where(DataSource.id.in_(agents)).values(folder_id=destination_id))

    paths, changed_rules = await _place_knowledge(
        db, org, user, removing, set(agents) if mode == "contents" else set(), mode, destination_id, require
    )
    if mode == "contents":
        service = DataSourceService()
        for aid in agents:
            await service.delete_data_source(db, aid, org, user)
    if changed_rules:
        from app.models.instruction import Instruction
        from app.services.instruction_service import InstructionService
        from app.services.instruction_version_service import InstructionVersionService

        service = InstructionService()
        build = await service.build_service.get_or_create_draft_build(db, org.id, source="user", user_id=user.id)
        for rule in await db.scalars(
            select(Instruction).where(Instruction.id.in_(changed_rules), Instruction.deleted_at.is_(None))
        ):
            await db.refresh(rule, ["data_sources", "labels", "references"])
            version = await InstructionVersionService().create_version(db, rule, user.id, commit=False)
            await service.build_service.add_to_build(db, build.id, rule.id, version.id)
        await service._auto_finalize_build(db, build, user, await service._get_user_permissions(db, user, org))
    await db.execute(
        delete(ResourceGrant).where(
            ResourceGrant.organization_id == org.id,
            ResourceGrant.resource_type == "folder",
            ResourceGrant.resource_id.in_(removing),
        )
    )
    for fid in reversed(removing):
        await db.execute(delete(Folder).where(Folder.id == fid))
    await audit_service.log(
        db,
        org.id,
        "folder.deleted",
        user.id,
        "folder",
        folder_id,
        details={"mode": mode, "folders": len(removing), "agents": len(agents)},
        commit=False,
    )
    return paths


async def _place_knowledge(db, org, user, removing, agents, mode, destination, require):
    from app.services.file_service import files_in_use, purge_files
    from app.services.instruction_service import InstructionService
    from app.services.test_suite_service import TestSuiteService

    paths = []
    changed_rules = set()
    for kind, (model, links, key, cap) in RESOURCES.items():
        items = list(
            await db.scalars(
                select(model).where(
                    model.organization_id == org.id,
                    (model.owner_folder_id.in_(removing))
                    | model.id.in_(select(key).where(links.c.folder_id.in_(removing))),
                )
            )
        )
        delete_rules, delete_files = [], []
        for item in items:
            if item.deleted_at:
                if item.owner_folder_id in removing:
                    item.owner_folder_id = None
                continue
            if kind == "instructions":
                changed_rules.add(item.id)
            outside = list(
                await db.scalars(
                    select(links.c.folder_id)
                    .where(key == item.id, links.c.folder_id.not_in(removing))
                    .order_by(links.c.folder_id)
                )
            )
            owned = item.owner_folder_id in removing
            if mode == "keep":
                if not destination:
                    raise HTTPException(409, "Choose a destination folder to keep the folder knowledge.")
                require("folder", [destination], cap)
                if destination not in outside:
                    from app.services.folder_knowledge import require_knowledge_authority

                    # Replacing a placement can grant a new audience access to shared content.
                    if kind != "files" or not owned:
                        if kind == "instructions":
                            await db.refresh(item, ["data_sources"])
                        await require_knowledge_authority(db, user, org, kind, item)
                    await db.execute(insert(links).values(folder_id=destination, **{key.name: item.id}))
                if owned:
                    item.owner_folder_id = destination
                continue
            if item.owner_folder_id and not owned:
                continue
            if outside:
                if owned:
                    # Changing the canonical owner requires authority in the new home.
                    from app.core.permission_resolver import resolve_permissions

                    rights = await resolve_permissions(db, user.id, org.id)
                    new_owner = next(
                        (fid for fid in outside if rights.has_resource_permission("folder", fid, cap)), None
                    )
                    if not new_owner:
                        raise HTTPException(403, "A shared item needs a manager in its remaining folder.")
                    item.owner_folder_id = new_owner
                continue
            if kind == "instructions":
                assoc = instruction_data_source_association
                other = await db.scalar(
                    select(assoc.c.data_source_id)
                    .where(assoc.c.instruction_id == item.id, assoc.c.data_source_id.not_in(agents))
                    .limit(1)
                )
                if not other and item.scope_mode == "targeted":
                    # An org-owned rule attached from global must survive removing its last folder.
                    from app.models.instruction_version import InstructionVersion

                    history = await db.execute(
                        select(
                            InstructionVersion.scope_mode,
                            InstructionVersion.folder_ids,
                            InstructionVersion.data_source_ids,
                        )
                        .where(InstructionVersion.instruction_id == item.id)
                        .order_by(InstructionVersion.version_number.desc())
                    )
                    was_global = False
                    for scope, folders, pins in history:
                        if pins:
                            break
                        if not folders:
                            was_global = scope == "global"
                            break
                    if not owned and was_global:
                        from app.core.permissions_decorator import require_org_permission

                        await require_org_permission(db, user.id, org.id, "manage_instructions")
                        item.scope_mode = "global"
                    else:
                        delete_rules.append(item.id)
            elif kind == "evals" and owned:
                targets = await db.scalars(
                    select(TestCase.data_source_ids_json).where(
                        TestCase.suite_id == item.id, TestCase.deleted_at.is_(None)
                    )
                )
                if any(set(ids or []) - agents for ids in targets) or (
                    item.data_source_id and item.data_source_id not in agents
                ):
                    raise HTTPException(409, "Move the shared eval suite to another folder first.")
                await TestSuiteService().delete_suite(db, org.id, user, item.id)
            elif kind == "files" and owned:
                ds = data_source_file_association
                shared = await db.scalar(
                    select(ds.c.file_id).where(ds.c.file_id == item.id, ds.c.data_source_id.not_in(agents)).limit(1)
                )
                for table in (project_file_association, report_file_association):
                    shared = shared or await db.scalar(
                        select(table.c.file_id).where(table.c.file_id == item.id).limit(1)
                    )
                if not shared and not await files_in_use(db, [item.id]):
                    delete_files.append(item)
            if owned:
                item.owner_folder_id = None
        await db.flush()
        if delete_rules:
            result = await InstructionService().bulk_delete_instructions(db, delete_rules, user, org)
            if result.failed_ids:
                raise HTTPException(409, "Could not delete all exclusive instructions.")
        await db.execute(delete(links).where(links.c.folder_id.in_(removing)))
        if delete_files:
            paths.extend(f.path for f in delete_files if f.path)
            await purge_files(db, delete_files, discard_bytes=False)
    return paths, changed_rules
