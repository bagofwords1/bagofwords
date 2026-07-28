"""Give existing draft builds the FULL snapshot a real build carries.

`seed_instructions_pending.py` gives each suggestion build a single BuildContent
row. Real builds don't look like that: `BuildService._copy_build_contents` copies
every instruction from main into the new build, so build_contents grows as
(builds × instructions) and only a handful of rows per build are an actual
change. That difference is the whole point of the pending-sweep cost — a seed
without it hides the scaling completely.

This backfills the carry-over rows onto already-seeded draft builds, turning a
"1 row per build" corpus into the production shape.

Usage:
  python scripts/seed_instructions_pending.py 4000 0.3    # instructions + thin drafts
  python scripts/fatten_pending_builds.py 260             # ...now realistic

  # 4,000 instructions × 260 pending builds ≈ 1.04M build_contents rows
"""
import os
import sys
import uuid
import asyncio

os.environ.setdefault("BOW_DATABASE_URL", "sqlite:///db/app.db")
os.environ.setdefault("BOW_SMTP_PASSWORD", "dummy")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select, insert, text as sqltext

import app.models  # noqa
import pkgutil, importlib
for _, modname, _ in pkgutil.iter_modules(app.models.__path__):
    if modname == "application":
        continue
    importlib.import_module(f"app.models.{modname}")

from app.models.instruction_build import InstructionBuild
from app.models.build_content import BuildContent


async def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 100

    url = os.environ["BOW_DATABASE_URL"]
    url = (url.replace("postgresql://", "postgresql+asyncpg://", 1) if url.startswith("postgresql://")
           else url.replace("sqlite:///", "sqlite+aiosqlite:///", 1) if url.startswith("sqlite:///") else url)
    engine = create_async_engine(url, future=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db:
        main_build = (await db.execute(
            select(InstructionBuild).where(InstructionBuild.is_main == True)  # noqa: E712
        )).scalars().first()
        assert main_build is not None, "seed instructions first (a main build must exist)"
        main_rows = (await db.execute(
            select(BuildContent.instruction_id, BuildContent.instruction_version_id)
            .where(BuildContent.build_id == str(main_build.id))
        )).all()
        print(f"main build carries {len(main_rows)} instructions")

        drafts = (await db.execute(
            select(InstructionBuild.id).where(
                InstructionBuild.is_main == False,  # noqa: E712
                InstructionBuild.status.in_(["draft", "pending_approval"]),
            ).limit(limit)
        )).scalars().all()
        print(f"fattening {len(drafts)} draft builds")

        for n, build_id in enumerate(drafts, 1):
            # The build's own (changed) rows stay as they are; everything else in
            # main is inherited unchanged — is_change=False, which is what
            # _copy_build_contents writes for a carry-over row.
            have = {str(i) for i in (await db.execute(
                select(BuildContent.instruction_id).where(BuildContent.build_id == str(build_id))
            )).scalars().all()}
            rows = [
                {"id": str(uuid.uuid4()), "build_id": str(build_id),
                 "instruction_id": str(iid), "instruction_version_id": str(vid),
                 "is_change": False}
                for iid, vid in main_rows if str(iid) not in have
            ]
            for i in range(0, len(rows), 5000):
                await db.execute(insert(BuildContent.__table__), rows[i:i + 5000])
            await db.commit()
            if n % 20 == 0:
                print(f"  ...{n}/{len(drafts)}")

        total = (await db.execute(sqltext("SELECT COUNT(*) FROM build_contents"))).scalar()
        changed = (await db.execute(
            sqltext("SELECT COUNT(*) FROM build_contents WHERE is_change = 1 OR is_change IS TRUE")
        )).scalar()
        print(f"build_contents: {total:,} rows, {changed:,} of them real changes")


if __name__ == "__main__":
    asyncio.run(main())
