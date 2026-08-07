"""Measure DELETE of one instruction that carries many pending suggestions.

`delete_instruction` calls `_void_pending_suggestions`, which walks every live
suggestion build on the instruction and runs `rebased_hunks_against_main`
(word-level 3-way difflib, quadratic in text length) for each one, then records
a resolved-hunk row per surviving hunk. So delete cost scales with
(suggestions on the row) x (text length^2) — on top of the build-copy cost that
every write pays.

Usage:
  python scripts/bench_delete_with_pending.py <n_suggestions> [text_chars]
"""
import os
import sys
import time
import uuid
import asyncio
import hashlib

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
from app.models.instruction_version import InstructionVersion
from app.models.instruction_build import InstructionBuild
from app.models.build_content import BuildContent
from app.services.instruction_service import InstructionService

COUNTER = {"n": 0}

WORDS = ("revenue orders customers refunds accounts region segment quarter "
         "margin cohort churn pipeline forecast attribution funnel").split()


def _text(n_chars: int, seed: int) -> str:
    import random
    rnd = random.Random(seed)
    out = []
    size = 0
    while size < n_chars:
        w = rnd.choice(WORDS)
        out.append(w)
        size += len(w) + 1
    return " ".join(out)


def _url():
    u = os.environ["BOW_DATABASE_URL"]
    if u.startswith("postgresql://"):
        return u.replace("postgresql://", "postgresql+asyncpg://", 1)
    if u.startswith("sqlite:///"):
        return u.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    return u


async def main():
    n_sugg = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    n_chars = int(sys.argv[2]) if len(sys.argv) > 2 else 3500

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
        org_id = str(org.id)

        main_build = (await db.execute(
            select(InstructionBuild).where(
                InstructionBuild.organization_id == org_id,
                InstructionBuild.is_main == True,  # noqa: E712
            )
        )).scalars().first()

        # Pick a target that is in main and not yet deleted.
        target = (await db.execute(
            select(Instruction)
            .join(BuildContent, BuildContent.instruction_id == Instruction.id)
            .where(Instruction.organization_id == org_id,
                   Instruction.deleted_at == None,  # noqa: E711
                   BuildContent.build_id == str(main_build.id))
            .limit(1)
        )).scalars().first()
        iid = str(target.id)

        # Give it a realistic-length live text on main (a fresh version pinned
        # into the main build), so the rebase has real work to do.
        live_text = _text(n_chars, seed=0)
        live_v = InstructionVersion(
            id=str(uuid.uuid4()), instruction_id=iid, text=live_text,
            title=target.title, status="published", version_number=900,
            content_hash=hashlib.sha256(live_text.encode()).hexdigest(),
            created_by_user_id=str(user.id),
        )
        db.add(live_v)
        await db.flush()
        main_row = (await db.execute(
            select(BuildContent).where(
                BuildContent.build_id == str(main_build.id),
                BuildContent.instruction_id == iid)
        )).scalars().first()
        main_row.instruction_version_id = live_v.id

        # N pending suggestion builds, each forked from a DIFFERENT base than
        # current main (main drifted after the fork) -- the expensive shape.
        base_text = _text(n_chars, seed=1)
        base_v = InstructionVersion(
            id=str(uuid.uuid4()), instruction_id=iid, text=base_text,
            title=target.title, status="published", version_number=901,
            content_hash=hashlib.sha256(base_text.encode()).hexdigest(),
            created_by_user_id=str(user.id),
        )
        db.add(base_v)
        await db.flush()

        next_bn = ((await db.execute(
            select(func.max(InstructionBuild.build_number))
            .where(InstructionBuild.organization_id == org_id)
        )).scalar() or 0) + 1

        for k in range(n_sugg):
            prop_text = _text(n_chars, seed=100 + k)
            pv = InstructionVersion(
                id=str(uuid.uuid4()), instruction_id=iid, text=prop_text,
                title=target.title, status="draft", version_number=1000 + k,
                content_hash=hashlib.sha256(prop_text.encode()).hexdigest(),
                created_by_user_id=str(user.id),
            )
            db.add(pv)
            b = InstructionBuild(
                id=str(uuid.uuid4()), build_number=next_bn + k, status="pending_approval",
                source="user", is_main=False, organization_id=org_id,
                created_by_user_id=str(user.id), base_build_id=str(main_build.id),
            )
            db.add(b)
            await db.flush()
            db.add(BuildContent(
                id=str(uuid.uuid4()), build_id=b.id, instruction_id=iid,
                instruction_version_id=pv.id, is_change=True,
                base_version_id=base_v.id,
            ))
        await db.commit()

        COUNTER["n"] = 0
        t0 = time.perf_counter()
        await svc.delete_instruction(db, iid, org, user)
        wall = time.perf_counter() - t0

        print(f"suggestions on row: {n_sugg:4d}   text: {n_chars:6d} chars"
              f"   ->  delete: {wall:7.2f}s   {COUNTER['n']} SQL statements")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
