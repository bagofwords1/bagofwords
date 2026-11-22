#!/usr/bin/env python3
"""Column usage performance probe: schema build/render, planner preparation and
per-step stats writes as ``column_stats`` grows.

Run against a COPY of the stack database (it inserts synthetic stats rows):

    cp backend/db/agent.db /tmp/perf.db
    cd backend && TESTING=true ENVIRONMENT=production TEST_DATABASE_URL=sqlite:////tmp/perf.db \
      uv run python ../tools/agent/column_stats_perf.py --steps 0,100000,300000 --out ../docs/feedback-loops/wide-table-columns/perf.json

For each target row count it tops ``column_stats`` up to that many rows
(spread over every table of every agent, real column names, random usage),
then measures per agent:

* ``build_ms`` / ``render_ms``: ``SchemaContextBuilder.build()`` and the
  planner's ``render_combined`` (median of --reps);
* ``prep_ms``: ``ContextHub.prime_static`` + ``refresh_warm`` + schema render,
  schema cache cleared (median of --reps);
* ``sql_statements``: statements one build issues (constant => no N+1);
* ``write_ms``: one step's column-usage write (catalog resolution, extraction,
  one batched upsert) for a representative query, median of --reps.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time
import uuid
from datetime import datetime, timedelta

WRITE_QUERIES = {
    "Wide Postgres": "SELECT host_name, AVG(net_retransmit_rate) AS r FROM public.metrics_wide_001 m "
                     "JOIN public.app_accounts a ON a.account_id = m.account_id WHERE a.region_code = 'north' "
                     "GROUP BY host_name ORDER BY r DESC LIMIT 5",
    "Wide DuckDB": "SELECT d.region_name, AVG(e.checkout_abandon_rate) FROM events_wide_5000 e "
                   "JOIN dim_region d ON d.region_code = e.region_code GROUP BY 1",
    "Wide Elastic": json.dumps({"index": "svc-events-wide", "size": 0,
                                "query": {"term": {"event.outcome": "failure"}},
                                "aggs": {"t": {"terms": {"field": "svc.request.tenant_tier.keyword"},
                                               "aggs": {"d": {"avg": {"field": "event.duration_ms"}}}}}}),
}


async def main(args):
    sys.path.insert(0, os.getcwd())
    import main  # noqa: F401
    from sqlalchemy import event, func, insert, select
    from sqlalchemy.orm import selectinload
    from app.dependencies import async_session_maker, engine
    from app.models.column_stats import ColumnStats
    from app.models.data_source import DataSource
    from app.models.datasource_table import DataSourceTable
    from app.models.organization import Organization
    from app.models.user import User
    from app.ai.context import context_hub as hub_mod
    from app.ai.context.builders.schema_context_builder import SchemaContextBuilder
    from app.services.column_usage_service import ColumnUsageService
    from app.services.organization_settings_service import OrganizationSettingsService

    counter = {"n": 0}

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _count(*_a, **_k):
        counter["n"] += 1

    rng = random.Random(215)
    results = {"steps": []}
    async with async_session_maker() as db:
        org = (await db.execute(select(Organization))).scalars().first()
        user = (await db.execute(select(User).where(User.email == args.email))).scalars().first()
        settings = await OrganizationSettingsService().get_settings(db, org, user)
        top_k = int(settings.get_config("top_k_schema").value)
        agents = {}
        for name in args.agents:
            ds = (await db.execute(select(DataSource).options(selectinload(DataSource.connections))
                                   .where(DataSource.name == name))).scalars().first()
            if ds:
                agents[name] = ds
        tables = (await db.execute(
            select(DataSourceTable.id, DataSourceTable.datasource_id, DataSourceTable.columns)
            .where(DataSourceTable.datasource_id.in_([str(d.id) for d in agents.values()]))
        )).all()
        table_cols = [(str(t[0]), str(t[1]), [c.get("name") for c in (t[2] or []) if isinstance(c, dict)]) for t in tables]

        for target in [int(x) for x in args.steps.split(",")]:
            have = int((await db.execute(select(func.count()).select_from(ColumnStats))).scalar() or 0)
            need = max(0, target - have)
            if need:
                # Spread over every table: wide tables get proportionally more rows.
                total_cols = sum(len(c) for _, _, c in table_cols) or 1
                existing = set((r[0], r[1]) for r in (await db.execute(
                    select(ColumnStats.datasource_table_id, ColumnStats.column_name))).all())
                batch = []
                now = datetime.utcnow()
                attempts = 0
                retired = 0
                while len(batch) < need and attempts < need * 20:
                    attempts += 1
                    tid, dsid, cols = rng.choices(table_cols, weights=[len(c) or 1 for _, _, c in table_cols])[0]
                    if not cols:
                        continue
                    col = rng.choice(cols)
                    if (tid, col) in existing:
                        # The catalog has fewer (table, column) pairs than the
                        # target: the rest are stats for columns that were
                        # dropped or renamed since (what a real org accumulates).
                        retired += 1
                        col = f"{col}__retired_{retired}"
                        if (tid, col) in existing:
                            continue
                    existing.add((tid, col))
                    u = rng.randint(1, 40)
                    s = rng.randint(0, u)
                    batch.append({
                        "id": str(uuid.uuid4()), "org_id": str(org.id), "data_source_id": dsid,
                        "datasource_table_id": tid, "column_name": col, "usage_count": u, "success_count": s,
                        "failure_count": u - s, "weighted_usage_count": s + 0.25 * (u - s),
                        "pos_feedback_count": rng.randint(0, 2), "neg_feedback_count": rng.randint(0, 1),
                        "weighted_pos_feedback": float(rng.randint(0, 2)), "weighted_neg_feedback": float(rng.randint(0, 1)),
                        "last_used_at": now - timedelta(days=rng.randint(0, 90)), "updated_at_stats": now,
                        "rank_weight": 0.0,
                        "created_at": now, "updated_at": now,
                    })
                for b in batch:
                    b["rank_weight"] = b["weighted_usage_count"] + b["weighted_pos_feedback"] - b["weighted_neg_feedback"]
                for i in range(0, len(batch), 2000):
                    await db.execute(insert(ColumnStats), batch[i:i + 2000])
                await db.commit()
            rows = int((await db.execute(select(func.count()).select_from(ColumnStats))).scalar() or 0)
            step = {"column_stats_rows": rows, "agents": {}}
            for name, ds in agents.items():
                build_ms, render_ms, prep_ms, write_ms = [], [], [], []
                stmts = None
                for _ in range(args.reps):
                    b = SchemaContextBuilder(db, [ds], org, None, user=user, organization_settings=settings)
                    c0 = counter["n"]
                    t0 = time.perf_counter()
                    ctx = await b.build()
                    t1 = time.perf_counter()
                    stmts = counter["n"] - c0
                    ctx.render_combined(top_k_per_ds=top_k, index_limit=1000)
                    t2 = time.perf_counter()
                    build_ms.append((t1 - t0) * 1000)
                    render_ms.append((t2 - t1) * 1000)
                for _ in range(args.reps):
                    hub_mod._SCHEMA_CACHE.clear()
                    hub = hub_mod.ContextHub(db=db, organization=org, report=None, data_sources=[ds], user=user,
                                             organization_settings=settings)
                    t0 = time.perf_counter()
                    await hub.prime_static(args.prompt)
                    await hub.refresh_warm()
                    hub.get_view().static.schemas.render_combined(top_k_per_ds=top_k, index_limit=1000)
                    prep_ms.append((time.perf_counter() - t0) * 1000)
                q = WRITE_QUERIES.get(name)
                svc = ColumnUsageService()
                written = 0
                for _ in range(args.reps if q else 0):
                    t0 = time.perf_counter()
                    usages = await svc.columns_for_step(db, [str(ds.id)], [q])
                    written = await svc.record_step_usage(db, org_id=str(org.id), usages=list(usages.values()), success=True)
                    write_ms.append((time.perf_counter() - t0) * 1000)
                med = lambda xs: round(statistics.median(xs), 1) if xs else None
                step["agents"][name] = {
                    "build_ms": med(build_ms), "render_ms": med(render_ms), "prep_ms": med(prep_ms),
                    "sql_statements_per_build": stmts, "write_ms": med(write_ms), "columns_written_per_step": written,
                }
                print(json.dumps({"rows": rows, name: step["agents"][name]}))
            results["steps"].append(step)
    if args.out:
        with open(args.out, "w") as f:
            json.dump(results, f, indent=2)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="0,100000")
    ap.add_argument("--reps", type=int, default=7)
    ap.add_argument("--agents", nargs="*", default=["Wide Postgres", "Wide DuckDB", "Wide Elastic"])
    ap.add_argument("--email", default=os.environ.get("BOW_ADMIN_EMAIL", "admin@example.com"))
    ap.add_argument("--prompt", default="Which hosts had the highest network retransmit rate last week?")
    ap.add_argument("--out", default="")
    asyncio.run(main(ap.parse_args()))
