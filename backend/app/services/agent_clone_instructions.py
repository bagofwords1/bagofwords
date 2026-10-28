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

import uuid
from datetime import datetime
from typing import Dict, List, Tuple

from fastapi import HTTPException
from sqlalchemy import case, func, insert, select, update

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


# Builds that are neither live nor finished: an edit waiting there was written
# against the version the clone is about to replace.
_OPEN_BUILD_STATUSES = ("draft", "pending_approval")


async def check_no_pending_changes(db, organization, items: List[Item]) -> None:
    """Linking gives each instruction a new version. A change already waiting
    in an open build was based on the old one, so promoting it later would
    make a version without the clone live again (the row keeps the clone's
    agent link and table references, but row and version would disagree).
    Refuse up front, naming the instructions, instead. Unchanged carry-over
    rows of open builds are moved onto the new version (rebase_open_builds)."""
    ids = [str(i.id) for i, _ in items]
    if not ids:
        return
    waiting = set(str(w) for w in (await db.execute(
        select(BuildContent.instruction_id)
        .join(InstructionBuild, InstructionBuild.id == BuildContent.build_id)
        .where(
            InstructionBuild.organization_id == str(organization.id),
            InstructionBuild.status.in_(_OPEN_BUILD_STATUSES),
            InstructionBuild.deleted_at.is_(None),
            BuildContent.is_change.is_(True),
            BuildContent.instruction_id.in_(ids),
        )
    )).scalars().all())
    blocked = [i.title or (i.text or "")[:60] for i, _ in items if str(i.id) in waiting]
    if blocked:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "instructions_pending",
                "message": "Some instructions have changes waiting for approval, so they can't be shared with the "
                           "new agent yet. Approve or reject those changes first, or copy the instructions.",
                "instructions": blocked,
            },
        )


def _version_row(instruction_id: str, number: int, v: InstructionVersion, *, user_id: str,
                 data_source_ids: List[str], references_json, label_ids) -> InstructionVersion:
    """A version with ``v``'s content and the given agents/references/labels.
    The hash is computed from the very fields stored, so they can't drift."""
    from app.services.instruction_version_service import InstructionVersionService

    data = {
        "text": v.text, "title": v.title, "description": v.description,
        "structured_data": v.structured_data, "formatted_content": v.formatted_content,
        "status": v.status, "load_mode": v.load_mode or "always",
        "applicable_modes": v.applicable_modes, "applicable_channels": v.applicable_channels,
        "references_json": references_json, "data_source_ids": data_source_ids,
        "label_ids": label_ids, "category_ids": v.category_ids,
    }
    return InstructionVersion(
        instruction_id=instruction_id, version_number=number, created_by_user_id=user_id,
        content_hash=InstructionVersionService().compute_content_hash(data), **data,
    )


def _row_sync(v: InstructionVersion) -> dict:
    """The row fields promote_build syncs from a newly live version, so the
    row and its current version agree (loaders that read the row see the same
    text the agent context reads from the version)."""
    values = {"text": v.text, "title": v.title, "description": v.description, "load_mode": v.load_mode,
              "applicable_modes": v.applicable_modes, "applicable_channels": v.applicable_channels}
    values = {k: val for k, val in values.items()
              if val is not None or k in ("description", "applicable_modes", "applicable_channels")}
    if v.category_ids:
        values["category"] = _version_category(v)
    if v.status == "published":
        values["status"] = "published"
    return values


def _version_category(v: InstructionVersion):
    """The row category promote_build derives from a version."""
    return v.category_ids[0] if isinstance(v.category_ids, list) else v.category_ids


async def _row_labels(db, ids: List[str]) -> Dict[str, List[str]]:
    """Labels live on the row only: editing them makes no version."""
    out: Dict[str, List[str]] = {}
    for iid, lid in (await db.execute(
        select(instruction_label_association.c.instruction_id, instruction_label_association.c.label_id)
        .where(instruction_label_association.c.instruction_id.in_(ids))
    )).all():
        out.setdefault(str(iid), []).append(str(lid))
    return out


async def rebase_open_builds(db, org_id: str, own_build_id: str, new_versions: Dict[str, str]) -> None:
    """Open builds copied from main hold the linked instructions' old version
    as unchanged carry-over rows; promoting one would make that version live
    again. Point those rows at the new version, as if copied after the link."""
    if not new_versions:
        return
    open_builds = (
        select(InstructionBuild.id)
        .where(InstructionBuild.organization_id == org_id, InstructionBuild.status.in_(_OPEN_BUILD_STATUSES),
               InstructionBuild.deleted_at.is_(None), InstructionBuild.id != own_build_id)
    )
    await db.execute(
        update(BuildContent)
        .where(BuildContent.build_id.in_(open_builds), BuildContent.is_change.is_(False),
               BuildContent.instruction_id.in_(list(new_versions)))
        .values(instruction_version_id=case(new_versions, value=BuildContent.instruction_id))
        .execution_options(synchronize_session=False)
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
            dir_map[str(d.id)] = str(uuid.uuid4())
            ctx.db.add(InstructionDirectory(
                id=dir_map[str(d.id)], organization_id=str(ctx.organization.id), data_source_id=ctx.clone_id,
                parent_id=dir_map.get(parent) if parent else None, name=d.name, position=d.position,
                created_by_user_id=str(ctx.user.id),
            ))
            queue.append(str(d.id))
    if dir_map:
        await ctx.db.flush()
    return dir_map


async def clone_instructions(ctx) -> None:
    from app.services.build_service import BuildService, MainBuildChangedError, _generate_build_title
    from app.services.instruction_service import InstructionService
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
        await check_no_pending_changes(db, ctx.organization, items)
    build = InstructionBuild(
        build_number=await bs._get_next_build_number(db, org_id), status="draft", source="user",
        is_main=False, base_build_id=str(main_id) if main_id else None,
        organization_id=org_id, created_by_user_id=user_id,
    )
    db.add(build)
    await db.flush()
    build.total_instructions = await bs._copy_contents_bulk(db, str(main_id), str(build.id), False) if main_id else 0

    current: List[dict] = []  # instruction id -> its new version, set after one flush
    row_labels = await _row_labels(db, [str(i.id) for i, _ in items])
    if mode == "copy":
        # Ids first, so references to another copied instruction can follow.
        for inst, _ in items:
            ctx.instruction_map[str(inst.id)] = str(uuid.uuid4())
        assoc, labels = [], []
        for inst, v in items:
            new_id = ctx.instruction_map[str(inst.id)]
            # The version is the content: the row mirrors it, as after a promote.
            db.add(Instruction(
                id=new_id, text=v.text, title=v.title, description=v.description,
                structured_data=v.structured_data, formatted_content=v.formatted_content,
                status=v.status, category=_version_category(v) if v.category_ids else inst.category,
                kind=inst.kind, load_mode=v.load_mode or "always",
                applicable_modes=v.applicable_modes, applicable_channels=v.applicable_channels,
                is_seen=inst.is_seen, can_user_toggle=inst.can_user_toggle,
                organization_id=org_id, user_id=user_id, source_type="user",
            ))
            assoc.append({"instruction_id": new_id, "data_source_id": ctx.clone_id})
            label_ids = row_labels.get(str(inst.id), [])
            labels += [{"instruction_id": new_id, "label_id": l} for l in label_ids]
            refs = _remap_refs(v.references_json, ctx)
            for r in refs:
                db.add(InstructionReference(instruction_id=new_id, object_type=r["object_type"], object_id=r["object_id"],
                                            column_name=r.get("column_name"), display_text=r.get("display_text")))
            version = _version_row(new_id, 1, v, user_id=user_id, data_source_ids=[ctx.clone_id],
                                   references_json=refs or None, label_ids=label_ids or None)
            version.id = str(uuid.uuid4())
            db.add(version)
            current.append({"id": new_id, "current_version_id": version.id})
            db.add(BuildContent(build_id=str(build.id), instruction_id=new_id,
                                instruction_version_id=version.id, is_change=True))
        await db.flush()
        await db.execute(insert(ids_assoc), assoc)
        if labels:
            await db.execute(insert(instruction_label_association), labels)
        build.added_count = len(items)
        build.total_instructions = (build.total_instructions or 0) + len(items)
        part.copied = len(items)
    else:
        ids = [str(i.id) for i, _ in items]
        agents = await _agents_by_instruction(db, ids)
        last = {str(k): n for k, n in (await db.execute(
            select(InstructionVersion.instruction_id, func.max(InstructionVersion.version_number))
            .where(InstructionVersion.instruction_id.in_(ids))
            .group_by(InstructionVersion.instruction_id)
        )).all()}
        contents = {str(iid): str(cid) for cid, iid in (await db.execute(
            select(BuildContent.id, BuildContent.instruction_id)
            .where(BuildContent.build_id == str(build.id), BuildContent.instruction_id.in_(ids))
        )).all()}
        content_updates = []
        for inst, v in items:
            iid = str(inst.id)
            added = []
            for r in v.references_json or []:
                if r.get("object_type") == "datasource_table" and str(r.get("object_id")) in ctx.table_map:
                    added.append({**r, "object_id": ctx.table_map[str(r["object_id"])]})
            for r in added:
                db.add(InstructionReference(instruction_id=iid, object_type=r["object_type"], object_id=r["object_id"],
                                            column_name=r.get("column_name"), display_text=r.get("display_text")))
            version = _version_row(
                iid, (last.get(iid) or 0) + 1, v, user_id=user_id,
                data_source_ids=sorted(set(agents.get(iid, [])) | {ctx.clone_id}),
                references_json=(list(v.references_json or []) + added) or None, label_ids=v.label_ids,
            )
            version.id = str(uuid.uuid4())
            db.add(version)
            current.append({"id": iid, "current_version_id": version.id, **_row_sync(version)})
            content_updates.append({"id": contents[iid], "instruction_version_id": version.id,
                                    "is_change": True, "base_version_id": str(v.id)})
            ctx.instruction_map[iid] = iid
        await db.execute(insert(ids_assoc), [{"instruction_id": i, "data_source_id": ctx.clone_id} for i in ids])
        await db.flush()
        await db.execute(update(BuildContent), content_updates)
        await rebase_open_builds(db, org_id, str(build.id),
                                 {c["id"]: c["current_version_id"] for c in current})
        build.modified_count = len(items)
        part.linked = len(items)
    await db.execute(update(Instruction), current)

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
