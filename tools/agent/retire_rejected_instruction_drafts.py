"""Retire draft instructions left behind by rejects that predate retirement.

Rejecting the suggestion that would have INTRODUCED an instruction used to
record the verdict on the build and leave the instruction row alone. The row
survived as a `draft`: absent from the main build, so invisible on every
build-scoped surface, yet still answering `search_instructions` — which is how
an agent came to report a rejected suggestion as an existing instruction, and
how its instruction count drifted above what the Agents page shows.

`InstructionService.reject_all_hunks` now retires such rows as it rejects them.
This backfills the ones already in the database.

The decision is delegated to `_retire_rejected_create_suggestion`, the same
helper the reject path calls, so this script cannot drift from the runtime
rule: a row is retired only when the main build does not carry it AND every
open proposal for it holds a terminal verdict.

Run from backend/ — reports without writing anything:
  uv run python ../tools/agent/retire_rejected_instruction_drafts.py

Add --apply to perform the soft delete:
  uv run python ../tools/agent/retire_rejected_instruction_drafts.py --apply
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.realpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "backend")
))

import main  # noqa: F401,E402


async def amain(apply: bool) -> int:
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.instruction import Instruction
    from app.models.organization import Organization
    from app.services.instruction_service import InstructionService

    service = InstructionService()
    retired, kept = [], []

    async with async_session_maker() as db:
        orgs = {
            str(o.id): o for o in
            (await db.execute(select(Organization))).unique().scalars().all()
        }
        drafts = (await db.execute(
            select(Instruction).where(
                Instruction.status == "draft",
                Instruction.deleted_at.is_(None),
            )
        )).unique().scalars().all()

        for instruction in drafts:
            org = orgs.get(str(instruction.organization_id))
            if org is None:
                continue
            label = (instruction.title or (instruction.text or "")[:48] or "(untitled)").strip()
            if await service._is_in_main_build(db, instruction):
                kept.append((instruction.id, label, "carried by the main build"))
                continue
            rows = await service._pending_suggestion_builds(db, str(instruction.id), org)
            if not rows:
                kept.append((instruction.id, label, "no proposal on record"))
                continue
            undecided = [
                build for build, _text, proposed_vid in rows
                if not service._voided_marker_matches(build, str(instruction.id))
                and not service._settled_marker_matches(
                    build, str(instruction.id),
                    str(proposed_vid) if proposed_vid else None,
                )
            ]
            if undecided:
                kept.append((instruction.id, label, f"{len(undecided)} proposal(s) still open"))
                continue
            retired.append((instruction.id, label))
            if apply:
                await service._retire_rejected_create_suggestion(
                    db, instruction, organization=org,
                )

    verb = "Retired" if apply else "Would retire"
    print(f"{verb} {len(retired)} draft instruction(s):")
    for iid, label in retired:
        print(f"  - {iid}  {label}")
    print(f"\nLeft alone ({len(kept)}):")
    for iid, label, why in kept:
        print(f"  - {iid}  {label}  — {why}")
    if not apply and retired:
        print("\nDry run. Re-run with --apply to perform the soft delete.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain(apply="--apply" in sys.argv)))
