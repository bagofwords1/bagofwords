"""Time the pending sweep + the three /agents tree-pane service calls against
whatever is currently seeded in db/app_sweep.db.

Usage: python scripts/profile_pending_snapshot_shape.py
"""
import os
import time
import asyncio

os.environ.setdefault("BOW_DATABASE_URL", "sqlite:///db/app_sweep.db")
os.environ.setdefault("BOW_SMTP_PASSWORD", "dummy")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

import app.models  # noqa
import pkgutil, importlib
for _, m, _ in pkgutil.iter_modules(app.models.__path__):
    if m != "application":
        importlib.import_module(f"app.models.{m}")

from app.models.user import User
from app.models.organization import Organization
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
        svc = InstructionService()

        async def timed(label, coro_factory):
            counter["n"] = 0
            t0 = time.perf_counter()
            res = await coro_factory()
            dt = time.perf_counter() - t0
            print(f"{label:<34} {dt*1000:9.1f} ms   sql={counter['n']}", flush=True)
            return res

        pending = await timed(
            "get_pending_change_instruction_ids",
            lambda: svc.get_pending_change_instruction_ids(db, organization=org, current_user=user),
        )
        print(f"    -> {len(pending)} pending instructions", flush=True)

        await timed(
            "get_instruction_counts",
            lambda: svc.get_instruction_counts(db, org, user),
        )

        res = await timed(
            "get_instructions(pending_only)",
            lambda: svc.get_instructions(
                db, org, user, skip=0, limit=200,
                include_own=True, include_drafts=True, include_archived=True,
                pending_only=True,
            ),
        )
        print(f"    -> {len(res['items'])} rows (total={res['total']})", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
