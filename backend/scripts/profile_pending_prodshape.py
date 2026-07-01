"""Detailed profiler for get_pending_change_instruction_ids at prod shape.

Times each sub-section of the sweep (candidate/sug_rows query, main_text query,
base_text query, python loop), counts SQL statements, and runs EXPLAIN QUERY PLAN
on the two heavy queries to expose scans due to missing indexes.

Usage: python scripts/profile_pending_prodshape.py
"""
import os, time, asyncio
os.environ.setdefault("BOW_DATABASE_URL", "sqlite:///db/app_sweep.db")
os.environ.setdefault("BOW_SMTP_PASSWORD", "dummy")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

from sqlalchemy import event, text, select, and_
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

import app.models  # noqa
import pkgutil, importlib
for _, m, _ in pkgutil.iter_modules(app.models.__path__):
    if m != "application":
        importlib.import_module(f"app.models.{m}")

from app.models.user import User
from app.models.organization import Organization
from app.models.instruction import Instruction
from app.models.instruction_build import InstructionBuild
from app.models.build_content import BuildContent
from app.models.instruction_version import InstructionVersion as _IV
from app.services.instruction_service import InstructionService


async def main():
    engine = create_async_engine("sqlite+aiosqlite:///db/app_sweep.db", future=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    counter = {"n": 0}

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):
        counter["n"] += 1

    async with Session() as db:
        user = (await db.execute(select(User).where(User.email == "sandbox@bow.dev"))).scalars().first()
        org = (await db.execute(select(Organization))).scalars().first()
        org_id = str(org.id)
        svc = InstructionService()

        # ---- Full sweep timing + SQL count ----
        counter["n"] = 0
        t0 = time.perf_counter()
        pending = await svc.get_pending_change_instruction_ids(db, organization=org, current_user=user)
        dt = time.perf_counter() - t0
        print(f"=== FULL SWEEP ===")
        print(f"pending instructions: {len(pending)}")
        print(f"wall time:            {dt*1000:.1f} ms")
        print(f"SQL statements fired: {counter['n']}")

        # ---- Sub-section timings (mirror the service body) ----
        print(f"\n=== SUB-SECTION TIMINGS ===")
        sug_where = [
            InstructionBuild.is_main.is_(False),
            InstructionBuild.organization_id == org_id,
            InstructionBuild.deleted_at.is_(None),
            InstructionBuild.status.in_(["draft", "pending_approval"]),
            InstructionBuild.source.in_(["user", "ai", "git"]),
        ]
        t = time.perf_counter()
        sug_rows = (await db.execute(
            select(BuildContent.instruction_id, InstructionBuild, _IV.text)
            .join(InstructionBuild, InstructionBuild.id == BuildContent.build_id)
            .join(_IV, _IV.id == BuildContent.instruction_version_id)
            .where(and_(*sug_where))
        )).all()
        print(f"(1) sug_rows query:   {(time.perf_counter()-t)*1000:8.1f} ms  ({len(sug_rows)} rows)")
        cand_ids = list({str(iid) for iid, _b, _t in sug_rows})

        t = time.perf_counter()
        rows2 = (await db.execute(
            select(BuildContent.instruction_id, _IV.text)
            .join(InstructionBuild, InstructionBuild.id == BuildContent.build_id)
            .join(_IV, _IV.id == BuildContent.instruction_version_id)
            .where(and_(
                InstructionBuild.is_main.is_(True),
                InstructionBuild.organization_id == org_id,
                InstructionBuild.deleted_at.is_(None),
                BuildContent.instruction_id.in_(cand_ids),
            ))
        )).all()
        print(f"(2) main_text query:  {(time.perf_counter()-t)*1000:8.1f} ms  ({len(rows2)} rows)")

        # ---- EXPLAIN QUERY PLAN on the two heavy queries ----
        print(f"\n=== EXPLAIN QUERY PLAN (raw SQL, sqlite) ===")
        q1 = text("""
            EXPLAIN QUERY PLAN
            SELECT bc.instruction_id, ib.id, iv.text
            FROM build_contents bc
            JOIN instruction_builds ib ON ib.id = bc.build_id
            JOIN instruction_versions iv ON iv.id = bc.instruction_version_id
            WHERE ib.is_main = 0 AND ib.organization_id = :org
              AND ib.deleted_at IS NULL
              AND ib.status IN ('draft','pending_approval')
              AND ib.source IN ('user','ai','git')
        """)
        print("--- Q1 sug_rows ---")
        for r in (await db.execute(q1, {"org": org_id})).all():
            print("   ", r)

        q2 = text("""
            EXPLAIN QUERY PLAN
            SELECT bc.instruction_id, iv.text
            FROM build_contents bc
            JOIN instruction_builds ib ON ib.id = bc.build_id
            JOIN instruction_versions iv ON iv.id = bc.instruction_version_id
            WHERE ib.is_main = 1 AND ib.organization_id = :org
              AND ib.deleted_at IS NULL
              AND bc.instruction_id IN ('x','y')
        """)
        print("--- Q2 main_text ---")
        for r in (await db.execute(q2, {"org": org_id})).all():
            print("   ", r)


if __name__ == "__main__":
    asyncio.run(main())
