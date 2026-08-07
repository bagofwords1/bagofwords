"""Break a single instruction save into its build-system phases.

Answers: how much of `update_instruction` is the full-snapshot fork
(`create_build` -> `_copy_build_contents`) versus everything else?
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
from app.services.instruction_service import InstructionService
from app.services import build_service as bs_mod
from app.schemas.instruction_schema import InstructionUpdate

TIMES = {}


def _instrument(cls, name):
    orig = getattr(cls, name)

    async def wrapped(self, *a, **k):
        t0 = time.perf_counter()
        try:
            return await orig(self, *a, **k)
        finally:
            TIMES[name] = TIMES.get(name, 0.0) + (time.perf_counter() - t0)
    setattr(cls, name, wrapped)


def _url():
    u = os.environ["BOW_DATABASE_URL"]
    if u.startswith("postgresql://"):
        return u.replace("postgresql://", "postgresql+asyncpg://", 1)
    if u.startswith("sqlite:///"):
        return u.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    return u


async def main():
    BS = bs_mod.BuildService
    for m in ("_copy_build_contents", "create_build", "get_or_create_draft_build",
              "add_to_build", "submit_build", "approve_build", "promote_build"):
        _instrument(BS, m)

    engine = create_async_engine(_url(), future=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    svc = InstructionService()

    async with Session() as db:
        user = (await db.execute(
            select(User).where(User.email == "sandbox@bow.dev"))).scalars().first()
        org = (await db.execute(select(Organization))).scalars().first()

        n = (await db.execute(
            select(func.count()).select_from(Instruction)
            .where(Instruction.organization_id == str(org.id),
                   Instruction.deleted_at == None))).scalar()  # noqa: E711
        rows = (await db.execute(
            select(func.count()).select_from(BuildContent))).scalar()

        target = (await db.execute(
            select(Instruction)
            .where(Instruction.organization_id == str(org.id),
                   Instruction.deleted_at == None)  # noqa: E711
            .limit(1))).scalars().first()

        t0 = time.perf_counter()
        await svc.update_instruction(
            db, str(target.id),
            InstructionUpdate(text=target.text + f" [e{time.time()}]"),
            org, user)
        total = time.perf_counter() - t0

    print(f"instructions={n}  build_contents={rows}")
    print(f"  update_instruction TOTAL      {total:7.3f}s")
    for k in ("get_or_create_draft_build", "create_build", "_copy_build_contents",
              "add_to_build", "submit_build", "approve_build", "promote_build"):
        if k in TIMES:
            print(f"    {k:<28} {TIMES[k]:7.3f}s  ({TIMES[k]/total*100:4.1f}%)")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
