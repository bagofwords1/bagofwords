"""Measure the cost of SAVING and DELETING one instruction as the workspace grows.

The read paths (list / counts / pending-changes) have already been profiled
(see docs/feedback-loops/agents-pending-reconciliation-perf.md). This bench
covers the *write* paths the customer reports as slow:

    PUT    /api/instructions/{id}      -> InstructionService.update_instruction
    DELETE /api/instructions/{id}      -> InstructionService.delete_instruction

Both walk the build system: get_or_create_draft_build -> add/remove_from_build
-> _auto_finalize_build -> submit -> approve -> promote. This reports wall time
and SQL statement count for a single save / single delete at a given workspace
size, so the scaling shape is visible.

Usage:
  python scripts/bench_instruction_write.py            # uses current db
"""
import os
import sys
import time
import asyncio

os.environ.setdefault("BOW_DATABASE_URL", "sqlite:///db/app.db")
os.environ.setdefault("BOW_SMTP_PASSWORD", "dummy")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

from sqlalchemy import event, select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

import app.models  # noqa
import pkgutil, importlib
for _, modname, _ in pkgutil.iter_modules(app.models.__path__):
    if modname == "application":
        continue
    importlib.import_module(f"app.models.{modname}")

from app.models.user import User
from app.models.organization import Organization
from app.models.instruction import Instruction
from app.models.instruction_build import InstructionBuild
from app.models.build_content import BuildContent
from app.services.instruction_service import InstructionService
from app.schemas.instruction_schema import InstructionUpdate


COUNTER = {"n": 0}


def _url():
    u = os.environ["BOW_DATABASE_URL"]
    if u.startswith("postgresql://"):
        return u.replace("postgresql://", "postgresql+asyncpg://", 1)
    if u.startswith("sqlite:///"):
        return u.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    return u


async def main():
    engine = create_async_engine(_url(), future=True)

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):
        COUNTER["n"] += 1

    Session = async_sessionmaker(engine, expire_on_commit=False)
    svc = InstructionService()

    async with Session() as db:
        user = (await db.execute(
            select(User).where(User.email == "sandbox@bow.dev")
        )).scalars().first()
        org = (await db.execute(select(Organization))).scalars().first()
        assert user and org, "run signup first (sandbox@bow.dev)"

        n_instr = (await db.execute(
            select(func.count()).select_from(Instruction)
            .where(Instruction.organization_id == str(org.id),
                   Instruction.deleted_at == None)  # noqa: E711
        )).scalar()
        n_builds = (await db.execute(
            select(func.count()).select_from(InstructionBuild)
            .where(InstructionBuild.organization_id == str(org.id))
        )).scalar()
        n_contents = (await db.execute(
            select(func.count()).select_from(BuildContent)
        )).scalar()
        main_rows = (await db.execute(
            select(func.count()).select_from(BuildContent)
            .join(InstructionBuild, InstructionBuild.id == BuildContent.build_id)
            .where(InstructionBuild.is_main == True)  # noqa: E712
        )).scalar()

        print(f"instructions:        {n_instr}")
        print(f"builds:              {n_builds}")
        print(f"build_contents rows: {n_contents}  (main build holds {main_rows})")

        targets = (await db.execute(
            select(Instruction)
            .where(Instruction.organization_id == str(org.id),
                   Instruction.deleted_at == None)  # noqa: E711
            .limit(2)
        )).scalars().all()
        assert len(targets) >= 2, "need at least 2 instructions"
        save_target, delete_target = targets[0], targets[1]

        # ---- SAVE ----
        COUNTER["n"] = 0
        t0 = time.perf_counter()
        await svc.update_instruction(
            db, str(save_target.id),
            InstructionUpdate(text=save_target.text + f" [edit {time.time()}]"),
            org, user,
        )
        save_wall = time.perf_counter() - t0
        save_sql = COUNTER["n"]

        # ---- DELETE ----
        COUNTER["n"] = 0
        t0 = time.perf_counter()
        await svc.delete_instruction(db, str(delete_target.id), org, user)
        del_wall = time.perf_counter() - t0
        del_sql = COUNTER["n"]

        print(f"\nsave   one instruction: {save_wall:6.2f}s   {save_sql:6d} SQL statements")
        print(f"delete one instruction: {del_wall:6.2f}s   {del_sql:6d} SQL statements")

        after = (await db.execute(
            select(func.count()).select_from(BuildContent)
        )).scalar()
        print(f"\nbuild_contents rows after 1 save + 1 delete: {after} "
              f"(+{after - n_contents})")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
