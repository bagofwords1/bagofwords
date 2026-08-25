"""Retire draft instructions left behind by rejects that predate retirement.

Rejecting the suggestion that would have INTRODUCED an instruction used to
record the verdict on the build and leave the instruction row alone. The row
survived as a `draft`: absent from the main build, so invisible on every
build-scoped surface, yet still answering `search_instructions` — which is how
an agent came to report a rejected suggestion as an existing instruction, and
how its instruction count drifted above what the Agents page shows.

`InstructionService.reject_all_hunks` now retires such rows as it rejects them.
This backfills the ones already in the database.

The decision is made by `_retirement_verdict` — the same predicate
`_retire_rejected_create_suggestion` calls on the reject path — so
this script cannot drift from the runtime rule: a row is retired only when its
org keeps a main build that does not carry it AND every open proposal for it
holds a terminal verdict. Nothing here re-implements any part of that test.

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
        candidates = (await db.execute(
            select(Instruction).where(Instruction.deleted_at.is_(None))
        )).unique().scalars().all()

        for instruction in candidates:
            org = orgs.get(str(instruction.organization_id))
            if org is None:
                continue
            label = (instruction.title or (instruction.text or "")[:48] or "(untitled)").strip()
            may_retire, why = await service._retirement_verdict(
                db, instruction, organization=org,
            )
            if not may_retire:
                # Healthy live rows are the overwhelming majority of a scan and
                # say nothing; only near-misses are worth a line.
                if why != InstructionService.RETIREMENT_REASON_LIVE:
                    kept.append((instruction.id, label, why))
                continue
            # The write path re-checks for itself. Report ITS answer, so a row
            # can never be printed as retired when the helper declined.
            if apply and not await service._retire_rejected_create_suggestion(
                db, instruction, organization=org,
            ):
                kept.append((instruction.id, label, "state changed mid-run; left alone"))
                continue
            retired.append((instruction.id, label))

    verb = "Retired" if apply else "Would retire"
    print(f"{verb} {len(retired)} orphaned instruction(s):")
    for iid, label in retired:
        print(f"  - {iid}  {label}")
    print(f"\nNear misses left alone ({len(kept)}) — live rows are not listed:")
    for iid, label, why in kept:
        print(f"  - {iid}  {label}  — {why}")
    if not apply and retired:
        print("\nDry run. Re-run with --apply to perform the soft delete.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain(apply="--apply" in sys.argv)))
