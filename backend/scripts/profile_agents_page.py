"""Profile the /agents page's heavy service calls: wall time, SQL statement
count, and serialized payload size for each.

Covers:
  * DataSourceService.get_data_sources         (GET /api/data_sources)
  * DataSourceService.get_active_data_sources  (GET /api/data_sources/active)
  * DataSourceService.get_data_source          (GET /api/data_sources/{id} — 2s poll)
  * ConnectionService list overview             (GET /api/connections)
  * InstructionService.review_hunks             (GET /instructions/{id}/review-hunks)
  * InstructionService.get_instruction_counts   (GET /api/instructions/counts)

Seed first with scripts/seed_agents_page_perf.py (+ seed_review_hunks_perf.py).

Usage: python scripts/profile_agents_page.py
"""
import os, time, json, asyncio
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
from app.models.data_source import DataSource
from app.models.instruction import Instruction


def _payload_bytes(result) -> int:
    def enc(o):
        if hasattr(o, "model_dump"):
            return o.model_dump()
        if hasattr(o, "isoformat"):
            return o.isoformat()
        if isinstance(o, set):
            return sorted(o)
        return str(o)
    try:
        return len(json.dumps(result, default=enc))
    except Exception:
        return -1


async def main():
    engine = create_async_engine("sqlite+aiosqlite:///db/app.db", future=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    counter = {"n": 0}

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):
        counter["n"] += 1

    async def bench(name, fn):
        # Fresh session per benchmark so identity-map caching doesn't help one
        # call at the expense of another.
        async with Session() as db:
            counter["n"] = 0
            t0 = time.perf_counter()
            result = await fn(db)
            dt = time.perf_counter() - t0
        size = _payload_bytes(result)
        n_items = len(result) if isinstance(result, (list, tuple)) else 1
        print(f"{name:34s} {dt:8.2f}s   {counter['n']:5d} SQL   "
              f"{size/1024:9.1f} KB   ({n_items} items)")
        return result

    async with Session() as db:
        user = (await db.execute(select(User).where(User.email == "sandbox@bow.dev"))).scalars().first()
        org = (await db.execute(select(Organization))).scalars().first()
        perf_ds_id = (await db.execute(
            select(DataSource.id).where(DataSource.name.like("Perf Agent 0%")).limit(1)
        )).scalar()
        hot_instr_id = (await db.execute(
            select(Instruction.id).where(Instruction.title == "Rule 0").limit(1)
        )).scalar()

    from app.services.data_source_service import DataSourceService
    from app.services.connection_service import ConnectionService
    from app.services.instruction_service import InstructionService
    ds_svc = DataSourceService()
    conn_svc = ConnectionService()
    instr_svc = InstructionService()

    print(f"{'endpoint (service call)':34s} {'wall':>9s}   {'stmts':>5s}   {'payload':>12s}")

    await bench("GET /data_sources",
                lambda db: ds_svc.get_data_sources(db, user, org))
    await bench("GET /data_sources/active",
                lambda db: ds_svc.get_active_data_sources(db, org, user, include_unconnected=True))
    if perf_ds_id:
        await bench("GET /data_sources/{id} (2s poll)",
                    lambda db: ds_svc.get_data_source(db, str(perf_ds_id), org, user))

    # GET /api/connections — the route loop lives in the route before the fix
    # and in ConnectionService after it; call whichever exists so this script
    # runs on both sides of the change.
    if hasattr(conn_svc, "get_connections_overview"):
        await bench("GET /connections (list)",
                    lambda db: conn_svc.get_connections_overview(db, org, user))
    else:
        from app.routes.connection import list_connections  # route fn, pre-fix
        await bench("GET /connections (list)",
                    lambda db: list_connections(current_user=user, db=db, organization=org))

    if hot_instr_id:
        await bench("GET .../review-hunks (hot instr)",
                    lambda db: instr_svc.review_hunks(db, str(hot_instr_id), organization=org, current_user=user))
    await bench("GET /instructions/counts",
                lambda db: instr_svc.get_instruction_counts(db, org, user))


if __name__ == "__main__":
    asyncio.run(main())
