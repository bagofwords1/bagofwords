"""Instructions for an agent clone: copy, link (share) or leave out.

Works on what the source agent USES today: instructions attached to it that
are live on the main build, read from their main (approved) version, not from
the instruction row, which can hold an edit still waiting for review.

- copy: a new instruction per source instruction, attached to the clone only.
  Table references point at the clone's copies of the tables; a reference to
  another copied instruction points at its copy; references to the source's
  dbt/LookML resources are dropped (they are not cloned). Same labels.
- link: the same instruction, now attached to the clone too, with a reference
  to the clone's table next to each table reference, and a new version so the
  agent context (which reads references from the version) sees them.

Both: the source's instruction folders are mirrored onto the clone with the
same placements, and the clone's primary instruction follows.

Everything goes into ONE dedicated build, approved and promoted inside the
caller's transaction: no shared draft build (someone else's pending edits
would ride along) and no service that commits.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Tuple

from fastapi import HTTPException
from sqlalchemy import insert, select, update

from app.core.main_build import main_build_select
from app.models.build_content import BuildContent
from app.models.instruction import Instruction, instruction_data_source_association as ids_assoc
from app.models.instruction_build import InstructionBuild
from app.models.instruction_directory import InstructionDirectory, InstructionDirectoryPlacement
from app.models.instruction_label import instruction_label_association
from app.models.instruction_reference import InstructionReference
from app.models.instruction_version import InstructionVersion

Item = Tuple[Instruction, InstructionVersion]


async def _main_build_id(db, org_id: str, *, lock: bool = False):
    q = main_build_select(org_id)
    if lock:
        q = q.with_for_update()
    return (await db.execute(q.limit(1))).scalar_one_or_none()


async def live_instructions(db, org_id: str, data_source_id: str) -> List[Item]:
    """Instructions attached to the agent and live on main, with their main
    version. Global instructions (no agent) are not the agent's."""
    main_id = await _main_build_id(db, org_id)
    if main_id is None:
        return []
    rows = (await db.execute(
        select(Instruction, InstructionVersion)
        .join(ids_assoc, ids_assoc.c.instruction_id == Instruction.id)
        .join(BuildContent, (BuildContent.instruction_id == Instruction.id) & (BuildContent.build_id == str(main_id)))
        .join(InstructionVersion, InstructionVersion.id == BuildContent.instruction_version_id)
        .where(
            ids_assoc.c.data_source_id == str(data_source_id),
            Instruction.organization_id == str(org_id),
            Instruction.deleted_at.is_(None),
        )
        .order_by(Instruction.created_at, Instruction.id)
    )).all()
    return [(i, v) for i, v in rows]


async def _agents_by_instruction(db, instruction_ids: List[str]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {i: [] for i in instruction_ids}
    if instruction_ids:
        for iid, ds in (await db.execute(
            select(ids_assoc.c.instruction_id, ids_assoc.c.data_source_id)
            .where(ids_assoc.c.instruction_id.in_(instruction_ids))
        )).all():
            out.setdefault(str(iid), []).append(str(ds))
    return out


async def check_link_authority(db, organization, user, source_id: str, items: List[Item]) -> None:
    """Linking adds the clone to each instruction, which then needs approval
    from someone who manages ALL its agents. Refuse up front (nothing written)
    when an instruction is also shared with an agent the cloner doesn't manage,
    instead of leaving a build waiting for approval."""
    from app.core.permission_resolver import FULL_ADMIN, resolve_permissions

    resolved = await resolve_permissions(db, str(user.id), str(organization.id))
    if FULL_ADMIN in resolved.org_permissions or resolved.has_org_permission("manage_instructions"):
        return
    agents = await _agents_by_instruction(db, [str(i.id) for i, _ in items])
    blocked = [
        i.title or (i.text or "")[:60]
        for i, _ in items
        if any(not resolved.has_resource_permission("data_source", ds, "manage_instructions")
               for ds in agents.get(str(i.id), []) if ds != str(source_id))
    ]
    if blocked:
        raise HTTPException(
            status_code=403,
            detail={
                "message": "Some instructions are shared with agents you don't manage, so they can't be shared "
                           "with the new agent. Copy the instructions instead, or ask someone who manages "
                           "those agents.",
                "instructions": blocked,
            },
        )


def _version_row(instruction_id: str, number: int, v: InstructionVersion, *, user_id: str,
                 data_source_ids: List[str], references_json, label_ids) -> InstructionVersion:
    from app.services.instruction_version_service import InstructionVersionService

    data = {
        "text": v.text, "title": v.title, "description": v.description,
        "structured_data": v.structured_data, "formatted_content": v.formatted_content,
        "status": v.status, "load_mode": v.load_mode,
        "applicable_modes": v.applicable_modes, "applicable_channels": v.applicable_channels,
        "references_json": references_json, "data_source_ids": data_source_ids,
        "label_ids": label_ids, "category_ids": v.category_ids,
    }
    return InstructionVersion(
        instruction_id=instruction_id, version_number=number,
        text=v.text, title=v.title, description=v.description, structured_data=v.structured_data,
        formatted_content=v.formatted_content, status=v.status, load_mode=v.load_mode or "always",
        applicable_modes=v.applicable_modes, applicable_channels=v.applicable_channels,
        references_json=references_json, data_source_ids=data_source_ids, label_ids=label_ids,
        category_ids=v.category_ids, content_hash=InstructionVersionService().compute_content_hash(data),
        created_by_user_id=user_id,
    )


def _remap_refs(refs, ctx) -> list:
    """References of a COPY: tables -> the clone's tables, copied instructions
    -> their copies; the source's metadata resources are dropped."""
    out = []
    for r in refs or []:
        kind, oid = r.get("object_type"), str(r.get("object_id"))
        if kind == "datasource_table":
            if oid not in ctx.table_map:
                continue
            oid = ctx.table_map[oid]
        elif kind == "metadata_resource":
            continue
        elif kind == "instruction":
            oid = ctx.instruction_map.get(oid, oid)
        out.append({**r, "object_id": oid})
    return out


async def _mirror_directories(ctx) -> Dict[str, str]:
    rows = (await ctx.db.execute(
        select(InstructionDirectory).where(
            InstructionDirectory.data_source_id == ctx.source_id, InstructionDirectory.deleted_at.is_(None)
        )
    )).scalars().all()
    by_parent: Dict[str, list] = {}
    for d in rows:
        by_parent.setdefault(str(d.parent_id) if d.parent_id else "", []).append(d)
    dir_map: Dict[str, str] = {}
    queue = [""]
    while queue:  # parents before children
        parent = queue.pop(0)
        for d in sorted(by_parent.get(parent, []), key=lambda x: (x.position or 0, x.name)):
            copy = InstructionDirectory(
                organization_id=str(ctx.organization.id), data_source_id=ctx.clone_id,
                parent_id=dir_map.get(parent) if parent else None, name=d.name, position=d.position,
                created_by_user_id=str(ctx.user.id),
            )
            ctx.db.add(copy)
            await ctx.db.flush()
            dir_map[str(d.id)] = str(copy.id)
            queue.append(str(d.id))
    return dir_map


async def clone_instructions(ctx) -> None:
    from app.services.build_service import BuildService, MainBuildChangedError, _generate_build_title
    from app.services.instruction_service import InstructionService
    from app.services.instruction_version_service import InstructionVersionService

    mode = ctx.opts.instructions
    part = ctx.part("instructions")
    if mode == "none":
        return
    db, org_id, user_id = ctx.db, str(ctx.organization.id), str(ctx.user.id)
    bs = BuildService()

    # Lock main FIRST and read what to clone under the lock: a promotion that
    # landed earlier is then part of what we read, and none can land between
    # this read and our claim (it waits for our commit).
    main_id = await _main_build_id(db, org_id, lock=True)
    items = await live_instructions(db, org_id, ctx.source_id)
    if not items:
        return
    if mode == "link":
        await check_link_authority(db, ctx.organization, ctx.user, ctx.source_id, items)
    build = InstructionBuild(
        build_number=await bs._get_next_build_number(db, org_id), status="draft", source="user",
        is_main=False, base_build_id=str(main_id) if main_id else None,
        organization_id=org_id, created_by_user_id=user_id,
    )
    db.add(build)
    await db.flush()
    build.total_instructions = await bs._copy_contents_bulk(db, str(main_id), str(build.id), False) if main_id else 0

    if mode == "copy":
        # Rows first, so references to another copied instruction can follow.
        for inst, _ in items:
            copy = Instruction(
                text=inst.text, title=inst.title, description=inst.description,
                structured_data=inst.structured_data, formatted_content=inst.formatted_content,
                status=inst.status, category=inst.category, kind=inst.kind, load_mode=inst.load_mode,
                applicable_modes=inst.applicable_modes, applicable_channels=inst.applicable_channels,
                is_seen=inst.is_seen, can_user_toggle=inst.can_user_toggle,
                organization_id=org_id, user_id=user_id, source_type="user",
            )
            db.add(copy)
            await db.flush()
            ctx.instruction_map[str(inst.id)] = str(copy.id)
        for inst, v in items:
            new_id = ctx.instruction_map[str(inst.id)]
            await db.execute(insert(ids_assoc).values(instruction_id=new_id, data_source_id=ctx.clone_id))
            for label_id in v.label_ids or []:
                await db.execute(insert(instruction_label_association).values(instruction_id=new_id, label_id=label_id))
            refs = _remap_refs(v.references_json, ctx)
            for r in refs:
                db.add(InstructionReference(instruction_id=new_id, object_type=r["object_type"], object_id=r["object_id"],
                                            column_name=r.get("column_name"), display_text=r.get("display_text")))
            version = _version_row(new_id, 1, v, user_id=user_id, data_source_ids=[ctx.clone_id],
                                   references_json=refs or None, label_ids=v.label_ids)
            db.add(version)
            await db.flush()
            # The version is the content: the row mirrors it, as after a promote.
            await db.execute(update(Instruction).where(Instruction.id == new_id).values(
                current_version_id=str(version.id), text=v.text, title=v.title, description=v.description,
                structured_data=v.structured_data, formatted_content=v.formatted_content, status=v.status,
                load_mode=v.load_mode or "always", applicable_modes=v.applicable_modes,
                applicable_channels=v.applicable_channels))
            db.add(BuildContent(build_id=str(build.id), instruction_id=new_id,
                                instruction_version_id=str(version.id), is_change=True))
        build.added_count = len(items)
        build.total_instructions = (build.total_instructions or 0) + len(items)
        part.copied = len(items)
    else:
        agents = await _agents_by_instruction(db, [str(i.id) for i, _ in items])
        vs = InstructionVersionService()
        for inst, v in items:
            iid = str(inst.id)
            await db.execute(insert(ids_assoc).values(instruction_id=iid, data_source_id=ctx.clone_id))
            added = []
            for r in v.references_json or []:
                if r.get("object_type") == "datasource_table" and str(r.get("object_id")) in ctx.table_map:
                    added.append({**r, "object_id": ctx.table_map[str(r["object_id"])]})
            for r in added:
                db.add(InstructionReference(instruction_id=iid, object_type=r["object_type"], object_id=r["object_id"],
                                            column_name=r.get("column_name"), display_text=r.get("display_text")))
            version = _version_row(
                iid, await vs.get_next_version_number(db, iid), v, user_id=user_id,
                data_source_ids=sorted(set(agents.get(iid, [])) | {ctx.clone_id}),
                references_json=(list(v.references_json or []) + added) or None, label_ids=v.label_ids,
            )
            db.add(version)
            await db.flush()
            await db.execute(update(Instruction).where(Instruction.id == iid).values(current_version_id=str(version.id)))
            await db.execute(update(BuildContent).where(
                BuildContent.build_id == str(build.id), BuildContent.instruction_id == iid,
            ).values(instruction_version_id=str(version.id), is_change=True, base_version_id=str(v.id)))
            ctx.instruction_map[iid] = iid
        build.modified_count = len(items)
        part.linked = len(items)

    # Folders and placements, mirrored onto the clone.
    dir_map = await _mirror_directories(ctx)
    if dir_map:
        for p in (await db.execute(
            select(InstructionDirectoryPlacement).where(InstructionDirectoryPlacement.directory_id.in_(list(dir_map)))
        )).scalars().all():
            target = ctx.instruction_map.get(str(p.instruction_id))
            if target:
                db.add(InstructionDirectoryPlacement(instruction_id=target, directory_id=dir_map[str(p.directory_id)]))
    primary = ctx.source.primary_instruction_id
    if primary and str(primary) in ctx.instruction_map:
        ctx.clone.primary_instruction_id = ctx.instruction_map[str(primary)]

    # Approve and make it main, in this transaction.
    await db.flush()
    isvc = InstructionService()
    if not await isvc._can_auto_publish_build(db, build, ctx.user, await isvc._get_user_permissions(db, ctx.user, ctx.organization)):
        raise HTTPException(status_code=403, detail="You can't publish instructions for the new agent.")
    build.title = _generate_build_title("user", added=build.added_count or 0, modified=build.modified_count or 0)
    build.status = "approved"
    build.approved_by_user_id = user_id
    build.approved_at = datetime.utcnow()
    try:
        await bs._claim_main(db, org_id, str(build.id), expected_main_build_id=main_id, enforce_expected_main=True)
    except MainBuildChangedError:
        raise HTTPException(status_code=409, detail={"code": "instructions_changed",
                                                      "message": "Instructions changed while cloning. Try again."})
    ctx.promoted_build_id = str(build.id)
