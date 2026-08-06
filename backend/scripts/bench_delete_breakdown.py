"""Break a single instruction delete into its phases.

Companion to bench_save_breakdown.py. Answers: after the void pass stopped
rebasing, what is a delete actually spending its time on?
"""
import os
import sys
import time
import asyncio

os.environ.setdefault("BOW_DATABASE_URL", "sqlite:///db/app.db")
os.environ.setdefault("BOW_SMTP_PASSWORD", "dummy")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

from sqlalchemy import select, func
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
from app.models.build_content import BuildContent
from app.models.instruction_build import InstructionBuild
from app.services.instruction_service import InstructionService
from app.services import build_service as bs_mod

TIMES = {}


def _instrument(cls, name, label=None):
    orig = getattr(cls, name)
    key = label or name

    async def wrapped(self, *a, **k):
        t0 = time.perf_counter()
        try:
            return await orig(self, *a, **k)
        finally:
            TIMES[key] = TIMES.get(key, 0.0) + (time.perf_counter() - t0)
    setattr(cls, name, wrapped)


async def main():
    _instrument(bs_mod.BuildService, "_copy_build_contents")
    _instrument(bs_mod.BuildService, "get_or_create_draft_build")
    _instrument(bs_mod.BuildService, "remove_from_build")
    _instrument(bs_mod.BuildService, "promote_build")
    _instrument(bs_mod.BuildService, "submit_build")
    _instrument(bs_mod.BuildService, "approve_build")
    _instrument(InstructionService, "_void_pending_suggestions")
    _instrument(InstructionService, "_auto_finalize_build")
    _instrument(InstructionService, "_get_user_permissions")

    eng = create_async_engine("sqlite+aiosqlite:///db/app.db", future=True)
    S = async_sessionmaker(eng, expire_on_commit=False)
    svc = InstructionService()
    async with S() as db:
        org = (await db.execute(select(Organization))).scalars().first()
        user = (await db.execute(select(User))).scalars().first()
        target = (await db.execute(
            select(BuildContent.instruction_id, func.count().label("n"))
            .join(InstructionBuild, InstructionBuild.id == BuildContent.build_id)
            .where(InstructionBuild.is_main == False,  # noqa: E712
                   InstructionBuild.deleted_at == None,  # noqa: E711
                   InstructionBuild.status.in_(["draft", "pending_approval"]),
                   BuildContent.is_change == True)  # noqa: E712
            .group_by(BuildContent.instruction_id)
            .order_by(func.count().desc())
            .limit(1)
        )).first()
        iid, n_sug = str(target[0]), target[1]

        t0 = time.perf_counter()
        await svc.delete_instruction(db, iid, org, user)
        total = time.perf_counter() - t0

    print(f"delete target {iid[:8]} with {n_sug} proposing builds")
    print(f"  delete_instruction TOTAL     {total:7.3f}s")
    for k in ("_get_user_permissions", "_void_pending_suggestions",
              "get_or_create_draft_build", "_copy_build_contents",
              "remove_from_build", "_auto_finalize_build", "submit_build",
              "approve_build", "promote_build"):
        if k in TIMES:
            print(f"    {k:<28} {TIMES[k]:7.3f}s  ({TIMES[k]/total*100:4.1f}%)")
    await eng.dispose()


if __name__ == "__main__":
    asyncio.run(main())
