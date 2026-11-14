"""Access to knowledge attached to an exact folder."""

from fastapi import HTTPException
from sqlalchemy import or_, select

from app.core.permission_resolver import resolve_permissions
from app.models.folder import Folder, folder_eval_suites, folder_files, folder_instructions
from app.models.instruction import Instruction, instruction_data_source_association
from app.models.file import File
from app.models.eval import TestSuite


RESOURCES = {
    "instructions": (Instruction, folder_instructions, folder_instructions.c.instruction_id, "manage_instructions"),
    "files": (File, folder_files, folder_files.c.file_id, "manage_files"),
    "evals": (TestSuite, folder_eval_suites, folder_eval_suites.c.suite_id, "manage_evals"),
}


async def require_capability(db, org, user, folder_id, capability):
    folder = await db.scalar(
        select(Folder.id).where(Folder.id == folder_id, Folder.organization_id == org.id, Folder.deleted_at.is_(None))
    )
    rights = await resolve_permissions(db, user.id, org.id)
    if not folder or not rights.has_resource_permission("folder", folder_id, capability):
        raise HTTPException(403, "Folder unavailable")
    return rights


def instruction_scope(agent_ids, folder_id=None):
    """Use the current live placement as an upper bound even for historical builds."""
    ds = instruction_data_source_association
    has_ds = select(ds.c.instruction_id).where(ds.c.instruction_id == Instruction.id).exists()
    clauses = [(Instruction.scope_mode == "global") & ~has_ds]
    if agent_ids is None:
        clauses.append(has_ds)
    elif agent_ids:
        clauses.append(
            select(ds.c.instruction_id)
            .where(ds.c.instruction_id == Instruction.id, ds.c.data_source_id.in_(agent_ids))
            .exists()
        )
    if folder_id:
        clauses.append(
            select(folder_instructions.c.folder_id)
            .where(folder_instructions.c.instruction_id == Instruction.id, folder_instructions.c.folder_id.in_([folder_id] if isinstance(folder_id, str) else folder_id))
            .exists()
        )
    return or_(*clauses)


async def report_folder_files(db, org, user, report):
    from app.models.report import report_folder_ids
    files = {}
    for fid in report_folder_ids(report):
        files.update({f.id: f for f in await folder_file_library(db, org, user, fid)})
    return list(files.values())


async def folder_file_library(db, org, user, folder_id):
    if not folder_id or not user:
        return []
    rights = await resolve_permissions(db, user.id, org.id)
    if not rights.has_resource_permission("folder", folder_id, "view"):
        return []
    from app.services.file_access_service import visible_files_clause

    visible = await visible_files_clause(db, user, org)
    return list(
        await db.scalars(
            select(File)
            .join(folder_files, folder_files.c.file_id == File.id)
            .where(
                folder_files.c.folder_id == folder_id,
                File.organization_id == org.id,
                File.deleted_at.is_(None),
                File.source_kind == "upload",
                visible,
            )
        )
    )


async def require_suite_placement_edit(db, organization_id, user, suite_id):
    placements = list(await db.scalars(select(folder_eval_suites.c.folder_id).where(
        folder_eval_suites.c.suite_id == suite_id)))
    if placements:
        rights = await resolve_permissions(db, user.id, organization_id)
        if any(not rights.has_resource_permission("folder", fid, "manage_evals") for fid in placements):
            raise HTTPException(403, "Suite unavailable")


async def instruction_folder_ids(db, instruction_id):
    return list(
        await db.scalars(
            select(folder_instructions.c.folder_id)
            .where(folder_instructions.c.instruction_id == instruction_id)
            .order_by(folder_instructions.c.folder_id)
        )
    )


async def readable_folder_rules(db, org, user, folder_id, ids=None):
    from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

    query = select(folder_instructions.c.instruction_id).where(folder_instructions.c.folder_id == folder_id)
    if ids is not None:
        query = query.where(folder_instructions.c.instruction_id.in_(ids))
    attached = list(await db.scalars(query))
    builder = InstructionContextBuilder(db, org, user, data_source_ids=[], folder_id=folder_id)
    return await builder.load_instructions_by_ids(attached, load_mode_filter=None)


async def record_knowledge_scope(db, report, org, user):
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from app.models.report import folder_report_knowledge

    from app.models.report import report_folder_ids
    for fid in report_folder_ids(report):
        files = await folder_file_library(db, org, user, fid)
        rules = await readable_folder_rules(db, org, user, fid)
        sources = [("folder", fid)] + [("files", f.id) for f in files] + [("instructions", r.id) for r in rules]
        sources += [("revision", r.version_id) for r in rules if r.version_id]
        insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
        await db.execute(
            insert(folder_report_knowledge)
            .values([dict(report_id=report.id, folder_id=fid, kind=k, item_id=i) for k, i in sources])
            .on_conflict_do_nothing()
        )


async def can_read_knowledge_scope(db, report, org, user, rights):
    from app.models.report import folder_report_knowledge as history

    rows = (
        await db.execute(
            select(history.c.folder_id, history.c.kind, history.c.item_id).where(history.c.report_id == report.id)
        )
    ).all()
    from app.models.report import report_folder_ids
    selected = set(report_folder_ids(report))
    for fid in {row.folder_id for row in rows}:
        curating = (report.mode == "training" and report.user_id == user.id and fid in selected
                    and rights.has_resource_permission("folder", fid, "manage_instructions"))
        if not curating and not rights.has_resource_permission("folder", fid, "view"):
            return False
        required_files = {r.item_id for r in rows if r.folder_id == fid and r.kind == "files"}
        if required_files and not required_files <= {f.id for f in await folder_file_library(db, org, user, fid)}:
            return False
        required_rules = {r.item_id for r in rows if r.folder_id == fid and r.kind == "instructions"}
        if required_rules:
            rules = await readable_folder_rules(db, org, user, fid, required_rules)
            if {r.id for r in rules} != required_rules:
                return False
            required_versions = {r.item_id for r in rows if r.folder_id == fid and r.kind == "revision"}
            from app.models.instruction_version import InstructionVersion

            if required_versions:
                source_versions = list(
                    await db.scalars(
                        select(InstructionVersion).where(
                            InstructionVersion.id.in_(required_versions),
                            InstructionVersion.instruction_id.in_(required_rules),
                        )
                    )
                )
                if len(source_versions) != len(required_versions):
                    return False
                table_ids = {
                    str(ref["object_id"])
                    for v in source_versions
                    for ref in v.references_json or []
                    if isinstance(ref, dict) and ref.get("object_type") == "datasource_table" and ref.get("object_id")
                }
                if table_ids:
                    from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

                    if await InstructionContextBuilder(db, org, user)._get_user_inaccessible_table_ids(table_ids):
                        return False
    return True


async def visible_instruction_folders(db, org, user):
    rights = await resolve_permissions(db, user.id, org.id)
    ids = select(Folder.id).where(Folder.organization_id == org.id, Folder.deleted_at.is_(None))
    if not rights.has_org_permission("full_admin_access"):
        ids = ids.where(
            Folder.id.in_(
                [
                    fid
                    for (kind, fid), perms in rights.resource_permissions.items()
                    if kind == "folder" and perms & {"view", "manage_instructions"}
                ]
            )
        )
    return Instruction.id.in_(
        select(folder_instructions.c.instruction_id).where(folder_instructions.c.folder_id.in_(ids))
    )


async def authoring_targets(db, org, user, report, capability, destination, folder_id, agent_ids):
    """Resolve an explicit write destination independently of the discovery roster."""
    from app.models.report import report_folder_ids
    selected = report_folder_ids(report)
    active = selected[0] if len(selected) == 1 else None
    if not selected and not destination and not folder_id and not agent_ids:
        return None  # Existing non-folder authoring contract.
    destination = destination or ("folder" if folder_id else "agents" if agent_ids else None)
    target_folder = folder_id if report is None or folder_id in selected else active
    if destination == "folder" and target_folder and not agent_ids and folder_id in (None, target_folder):
        await require_capability(db, org, user, target_folder, capability)
        return target_folder, []
    rights = await resolve_permissions(db, user.id, org.id)
    if destination == "global" and not folder_id and not agent_ids and rights.has_org_permission(capability):
        return None, []
    if destination == "agents" and agent_ids and not folder_id:
        from app.ai.tools.implementations.agent_focus_common import resolve_run_agents
        if report is None:
            from app.models.data_source import DataSource
            allowed = set(await db.scalars(select(DataSource.id).where(DataSource.id.in_(agent_ids),
                DataSource.organization_id == org.id, DataSource.deleted_at.is_(None))))
        else:
            allowed = {a.id for a in await resolve_run_agents(db, org, user, report)}
        if all(a in allowed and rights.has_resource_permission("data_source", a, capability) for a in agent_ids):
            return None, list(dict.fromkeys(agent_ids))
    raise HTTPException(403, "Choose an authorized destination: selected folder, specific agents, or organization. Ask the user if unclear.")
