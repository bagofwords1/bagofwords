"""The one place a scope change reaches an instruction's LIVE attachment.

Both ways a scope change goes live share this applier, so they can never
drift apart (they once differed on how references to a deleted table were
handled):

  - reviewer accept — InstructionService computes the delta from the accepted
    hunks and calls this directly;
  - build promote — BuildService._apply_scope_on_promote computes each changed
    instruction's base->proposed delta and calls this per instruction.

A delta, never a set: additions are idempotent (an agent or reference already
live is left alone) and removals only touch rows actually present, so a delta
computed against a possibly-stale base can never clobber scope the delta did
not name. Writes only — the caller commits.
"""

from sqlalchemy import delete as sql_delete, select
from sqlalchemy.ext.asyncio import AsyncSession


async def apply_scope_delta(
    db: AsyncSession,
    instruction_id: str,
    added_ds,
    removed_ds,
    added_refs: list,
    removed_refs: list,
) -> None:
    """Attach ``added_ds``, detach ``removed_ds``, add ``added_refs`` and drop
    ``removed_refs`` on ``instruction_id``'s live attachment. ``added_ds`` is
    expected to hold existing (non-deleted) agents only; the caller filters."""
    from app.models.instruction import instruction_data_source_association as idsa
    from app.models.instruction_reference import InstructionReference
    from app.models.datasource_table import DataSourceTable
    from app.models.data_source import DataSource

    iid = str(instruction_id)
    # Drop references to tables whose data source was deleted since the change
    # was staged — their rows (and FK targets) may be gone. Done HERE so both
    # callers (reviewer accept and build promote) agree: promote used to add a
    # dangling InstructionReference that accept filtered out.
    added_refs = list(added_refs or [])
    if added_refs:
        ref_ids = sorted({str(r.get("object_id")) for r in added_refs if r.get("object_id")})
        alive = {str(tid) for (tid,) in (await db.execute(
            select(DataSourceTable.id)
            .join(DataSource, DataSource.id == DataSourceTable.datasource_id)
            .where(DataSourceTable.id.in_(ref_ids), DataSource.deleted_at.is_(None))
        )).all()} if ref_ids else set()
        added_refs = [r for r in added_refs if str(r.get("object_id")) in alive]
    add_ds = {str(d) for d in (added_ds or [])}
    rem_ds = {str(d) for d in (removed_ds or [])} - add_ds
    attached = {str(d) for (d,) in (await db.execute(
        select(idsa.c.data_source_id).where(idsa.c.instruction_id == iid)
    )).all()}
    new = sorted(add_ds - attached)
    if new:
        await db.execute(idsa.insert(), [{"instruction_id": iid, "data_source_id": d} for d in new])
    gone = sorted(rem_ds & attached)
    if gone:
        await db.execute(sql_delete(idsa).where(
            idsa.c.instruction_id == iid, idsa.c.data_source_id.in_(gone)))
    if not (added_refs or removed_refs):
        await db.flush()
        return
    have = {(r.object_type, str(r.object_id), r.column_name): r for r in (await db.execute(
        select(InstructionReference).where(InstructionReference.instruction_id == iid)
    )).scalars().all()}
    for r in (added_refs or []):
        ident = (r.get("object_type") or "datasource_table", str(r.get("object_id")), r.get("column_name"))
        if not r.get("object_id") or ident in have:
            continue
        row = InstructionReference(
            instruction_id=iid, object_type=ident[0], object_id=str(r["object_id"]),
            column_name=r.get("column_name"), relation_type=r.get("relation_type"),
            display_text=r.get("display_text"),
        )
        db.add(row)
        have[ident] = row
    for r in (removed_refs or []):
        ident = (r.get("object_type") or "datasource_table", str(r.get("object_id")), r.get("column_name"))
        row = have.pop(ident, None)
        if row is not None:
            await db.delete(row)
    await db.flush()
