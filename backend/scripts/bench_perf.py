"""Benchmark suite for cascade-prone endpoints + context builders.

Endpoints: curl warm-p50 over 3 runs.
Context builders: count SQL statements fired when building context.
"""
import os
import asyncio
import json
import time
import statistics
import subprocess
import sys

os.environ.setdefault("BOW_DATABASE_URL", "sqlite:///db/app.db")
os.environ.setdefault("BOW_SMTP_PASSWORD", "dummy")

TOKEN = open("/tmp/token.txt").read().strip()
ORG = open("/tmp/org.txt").read().strip()
REPORT_ID = open("/tmp/report_id.txt").read().strip()
INST_ID = open("/tmp/inst_id.txt").read().strip()
DS_ID = open("/tmp/ds_id.txt").read().strip()

ENDPOINTS = [
    ("/api/data_sources/active",        "GET", "data_sources_active"),
    (f"/api/data_sources/{DS_ID}",      "GET", "data_source_singular"),
    (f"/api/instructions/{INST_ID}",    "GET", "instruction_singular"),
    ("/api/instructions?limit=50",      "GET", "instructions_list"),
    ("/api/reports?limit=10",           "GET", "reports_list"),
    (f"/api/reports/{REPORT_ID}",       "GET", "report_singular"),
]


def bench_endpoint(url: str, method: str, label: str, n: int = 10) -> dict:
    times_ms = []
    for _ in range(n):
        t0 = time.perf_counter()
        r = subprocess.run([
            "curl", "-sS", "-X", method, "-o", "/dev/null", "-w",
            "%{time_total}|%{http_code}|%{size_download}",
            f"http://localhost:8000{url}",
            "-H", f"Authorization: Bearer {TOKEN}",
            "-H", f"X-Organization-Id: {ORG}",
        ], capture_output=True, text=True, timeout=30)
        time_s, code, size = r.stdout.strip().split("|")
        if code != "200":
            return {"label": label, "url": url, "error": f"status {code}"}
        times_ms.append(float(time_s) * 1000)
    return {
        "label": label,
        "url": url,
        "p50_ms": round(statistics.median(times_ms), 1),
        "min_ms": round(min(times_ms), 1),
        "max_ms": round(max(times_ms), 1),
        "bytes": int(size),
    }


async def bench_context_builders():
    """Count SQL statements fired when building agent_v2 context.

    Targets the per-completion hot path: instruction_context_builder and
    entity_context_builder. We don't run agent_v2 end-to-end (would need
    an LLM); we directly invoke the context-build code paths and count
    statements via the sync engine event listener.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy import event
    from sqlalchemy.future import select
    import app.models  # noqa
    import pkgutil, importlib
    for _, modname, _ in pkgutil.iter_modules(app.models.__path__):
        if modname == "application":
            continue
        importlib.import_module(f"app.models.{modname}")

    from app.dependencies import async_session_maker, engine
    from app.models.organization import Organization
    from app.models.user import User
    from app.models.instruction import Instruction
    from app.models.entity import Entity

    counter = {"n": 0}

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _on(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    async with async_session_maker() as db:
        org = (await db.execute(select(Organization).where(Organization.id == ORG))).scalars().first()
        user = (await db.execute(select(User).where(User.email == "sandbox@bow.dev"))).scalars().first()
        assert org and user, "missing org/user"

        results = {}

        from app.ai.context.builders.instruction_context_builder import (
            InstructionContextBuilder,
        )
        from app.ai.context.builders.entity_context_builder import (
            EntityContextBuilder,
        )

        from sqlalchemy.orm import selectinload as _selectinload, lazyload as _lazyload2
        from app.models.instruction import Instruction as I
        from app.models.entity import Entity as E

        # 1a. Instruction baseline (replicate OLD code's query — no lazyload)
        counter["n"] = 0
        t0 = time.perf_counter()
        await db.execute(
            select(I).options(
                _selectinload(I.user),
                _selectinload(I.data_sources),
                _selectinload(I.labels),
            ).where(
                I.status == "published",
                I.organization_id == org.id,
                I.deleted_at.is_(None),
            )
        )
        baseline_inst_ms = (time.perf_counter() - t0) * 1000
        baseline_inst_sql = counter["n"]
        # 1b. Instruction fixed (real production code path)
        counter["n"] = 0
        t0 = time.perf_counter()
        builder = InstructionContextBuilder(db, org, user)
        always = await builder.load_always_instructions()
        fix_ms = (time.perf_counter() - t0) * 1000
        results["instruction_context_build"] = {
            "sql_stmts": counter["n"],
            "wall_ms": round(fix_ms, 1),
            "rows": len(always),
            "baseline_sql": baseline_inst_sql,
            "baseline_ms": round(baseline_inst_ms, 1),
        }

        # 2a. Entity baseline (replicate OLD code — no lazyload)
        counter["n"] = 0
        t0 = time.perf_counter()
        await db.execute(
            select(E).options(_selectinload(E.data_sources))
            .where(E.organization_id == org.id, E.deleted_at.is_(None))
        )
        baseline_ent_ms = (time.perf_counter() - t0) * 1000
        baseline_ent_sql = counter["n"]
        # 2b. Entity fixed (real production code path)
        counter["n"] = 0
        t0 = time.perf_counter()
        ent_builder = EntityContextBuilder(db, org)
        ents = await ent_builder.load_entities(keywords=["sales", "revenue"], data_source_ids=[DS_ID])
        fix_ms = (time.perf_counter() - t0) * 1000
        results["entity_context_build"] = {
            "sql_stmts": counter["n"],
            "wall_ms": round(fix_ms, 1),
            "rows": len(ents),
            "baseline_sql": baseline_ent_sql,
            "baseline_ms": round(baseline_ent_ms, 1),
        }

        # 3. Bare select(Report) — agent_v2.py:1116 pattern
        from app.models.report import Report
        from sqlalchemy.orm import lazyload as _lazyload
        # baseline-shaped query (no options) — what we WERE doing
        counter["n"] = 0
        t0 = time.perf_counter()
        rep_baseline = (await db.execute(
            select(Report).where(Report.id == REPORT_ID)
        )).scalars().first()
        baseline_ms = (time.perf_counter() - t0) * 1000
        baseline_sql = counter["n"]
        # fixed-shaped query — what we ARE doing now
        counter["n"] = 0
        t0 = time.perf_counter()
        rep_fixed = (await db.execute(
            select(Report).where(Report.id == REPORT_ID).options(_lazyload("*"))
        )).scalars().first()
        results["report_singular_query"] = {
            "sql_stmts": counter["n"],
            "wall_ms": round((time.perf_counter() - t0) * 1000, 1),
            "rows": 1 if rep_fixed else 0,
            "baseline_sql": baseline_sql,
            "baseline_ms": round(baseline_ms, 1),
        }

        return results


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    print(f"=== {label} ===")
    endpoint_results = [bench_endpoint(u, m, l) for u, m, l in ENDPOINTS]
    for r in endpoint_results:
        if "error" in r:
            print(f"  {r['label']:<25} ERROR: {r['error']}")
        else:
            print(f"  {r['label']:<25} p50={r['p50_ms']:>7.1f}ms  min={r['min_ms']:>6.1f}  max={r['max_ms']:>6.1f}  {r['bytes']}B")

    ctx_results = asyncio.run(bench_context_builders())
    print()
    for name, r in ctx_results.items():
        bs = r.get("baseline_sql"); bm = r.get("baseline_ms")
        line = f"  {name:<28} SQL={r['sql_stmts']:>3}  wall={r['wall_ms']:>7.1f}ms  rows={r['rows']}"
        if bs is not None:
            line += f"   (baseline: SQL={bs}  {bm}ms)"
        print(line)

    out_path = f"/tmp/bench_{label}.json"
    with open(out_path, "w") as f:
        json.dump({"endpoints": endpoint_results, "ctx": ctx_results}, f, indent=2)
    print(f"\nsaved → {out_path}")


if __name__ == "__main__":
    main()
