"""Profile get_pending_change_instruction_ids: wall time + SQL statement count.

Confirms the bottleneck is the per-instruction review_hunks loop (O(N) queries),
not a single heavy query.

Usage: python scripts/profile_pending_changes.py
"""
import os, time, asyncio
os.environ.setdefault("BOW_DATABASE_URL", "sqlite:///db/app.db")
os.environ.setdefault("BOW_SMTP_PASSWORD", "dummy")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select

import app.models  # noqa
import pkgutil, importlib
for _, m, _ in pkgutil.iter_modules(app.models.__path__):
    if m != "application":
        importlib.import_module(f"app.models.{m}")

from app.models.user import User
from app.models.organization import Organization
from app.services.instruction_service import InstructionService


async def main():
    engine = create_async_engine("sqlite+aiosqlite:///db/app.db", future=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    counter = {"n": 0}
    # Count SQL on the underlying sync engine that the async engine drives.
    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):
        counter["n"] += 1

    async with Session() as db:
        user = (await db.execute(select(User).where(User.email == "sandbox@bow.dev"))).scalars().first()
        org = (await db.execute(select(Organization))).scalars().first()
        svc = InstructionService()

        counter["n"] = 0
        t0 = time.perf_counter()
        pending = await svc.get_pending_change_instruction_ids(db, organization=org, current_user=user)
        dt = time.perf_counter() - t0

    print(f"pending instructions: {len(pending)}")
    print(f"wall time:            {dt:.2f}s")
    print(f"SQL statements fired: {counter['n']}")
    print(f"queries per pending:  {counter['n']/max(1,len(pending)):.1f}")


if __name__ == "__main__":
    asyncio.run(main())
