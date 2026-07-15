"""Profile the diagnosis service calls: SQL statement counts, rows pulled into
Python, index usage, and a SQL-side GROUP BY comparison for the timeseries.

Calls ConsoleService methods directly (same code the endpoints run) on the
seeded sandbox DB, with an instrumented engine.

Usage:  BOW_DATABASE_URL=sqlite:///db/app.db uv run python scripts/profile_diagnosis_perf.py
"""
import asyncio
import sqlite3
import time

from sqlalchemy import event, select, func, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

# Register every ORM model so string-named relationships resolve
import importlib
import pkgutil

import app.models as _models_pkg

for _m in pkgutil.iter_modules(_models_pkg.__path__):
    if _m.name == "application":
        continue  # dead module: references undefined DataSourceApplicationAssociation
    importlib.import_module(f"app.models.{_m.name}")

from app.models.organization import Organization
from app.schemas.console_schema import MetricsQueryParams
from app.services.console_service import ConsoleService

DB = "db/app.db"

stmt_count = {"n": 0}


async def main():
    engine = create_async_engine(f"sqlite+aiosqlite:///{DB}")

    @event.listens_for(engine.sync_engine, "after_cursor_execute")
    def count(conn, cursor, statement, parameters, context, executemany):
        stmt_count["n"] += 1

    async with AsyncSession(engine) as db:
        org = (await db.execute(select(Organization))).scalars().first()
        svc = ConsoleService()
        params = MetricsQueryParams(start_date=None, end_date=None, data_source_ids=None)

        n_ae = (await db.execute(select(func.count()).select_from(text("agent_executions")))).scalar()
        print(f"agent_executions rows: {n_ae:,}\n")

        # --- timeseries: current implementation ---
        stmt_count["n"] = 0
        t0 = time.perf_counter()
        result = await svc.get_diagnosis_timeseries(db, org, params)
        t1 = time.perf_counter()
        print(f"get_diagnosis_timeseries:      {(t1-t0)*1000:8,.0f} ms, "
              f"{stmt_count['n']} SQL stmts, returns {len(result.points)} points "
              f"(after fetching every AE row + every failed-tool AE id into Python)")

        # --- timeseries: what a server-side GROUP BY costs on the same data ---
        t0 = time.perf_counter()
        rows = (await db.execute(text("""
            SELECT date(created_at) d,
                   SUM(CASE WHEN status='error' OR id IN (
                       SELECT DISTINCT agent_execution_id FROM tool_executions WHERE success=0
                   ) THEN 1 ELSE 0 END) errors,
                   COUNT(*) total
            FROM agent_executions
            WHERE organization_id = :org AND created_at >= datetime('now','-30 days')
            GROUP BY date(created_at) ORDER BY d
        """), {"org": org.id})).all()
        t1 = time.perf_counter()
        print(f"same aggregation in SQL:       {(t1-t0)*1000:8,.0f} ms, "
              f"1 SQL stmt, returns {len(rows)} rows")

        # --- summaries page 1 ---
        stmt_count["n"] = 0
        t0 = time.perf_counter()
        await svc.get_agent_execution_summaries(db, org, params, page=1, page_size=10)
        t1 = time.perf_counter()
        print(f"\nget_agent_execution_summaries: {(t1-t0)*1000:8,.0f} ms, "
              f"{stmt_count['n']} SQL stmts for 10 rows "
              f"(incl. 2 identical full COUNT(*) subquery scans — one is dead code)")

        # --- KPI metrics ---
        stmt_count["n"] = 0
        t0 = time.perf_counter()
        await svc.get_diagnosis_dashboard_metrics(db, org, params)
        t1 = time.perf_counter()
        print(f"get_diagnosis_dashboard_metrics:{(t1-t0)*1000:7,.0f} ms, "
              f"{stmt_count['n']} SQL stmts (5 sequential COUNT-with-join queries "
              f"over the full date range)")

    await engine.dispose()

    # --- index evidence: the composite index from perfidx01 IS used, so the
    # remaining cost is genuinely per-row work over the whole date range ---
    c = sqlite3.connect(DB)
    print("\nEXPLAIN QUERY PLAN — the base filter every diagnosis query uses:")
    for row in c.execute(
        "EXPLAIN QUERY PLAN SELECT count(*) FROM agent_executions "
        "WHERE organization_id=? AND created_at>=datetime('now','-30 days')",
        (org.id,),
    ):
        print("  ", row)
    idx = [r[1] for r in c.execute("PRAGMA index_list(agent_executions)").fetchall()]
    print(f"indexes on agent_executions: {idx}")


asyncio.run(main())
