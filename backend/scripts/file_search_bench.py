"""Benchmark file search on a generated file share, through the real stack.

    corpus on disk → network_dir connection → the real index run
    (DataSourceService.refresh_data_source_schema → NetworkDirClient.get_schemas)
    → every golden query through the real `search_files` agent tool.

Reports, per query kind and overall: how often the expected file is the first
result / in the top 5 / in the top 10, mean reciprocal rank, latency, and how
the tool answered (index or live scan) — plus index build time and size.

Run from backend/ with the database to use in BOW_DATABASE_URL (the schema is
migrated first):

    BOW_DATABASE_URL=sqlite:///db/bench.db \\
      uv run python scripts/file_search_bench.py --corpus /data/bench-corpus \\
        --files 50000 --mode fulltext --out fulltext.json

`--mode` is the connection's Indexing setting: content (keywords) | fulltext.
Compare two runs with `--compare a.json b.json`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _migrate() -> None:
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True,
                   cwd=str(Path(__file__).resolve().parents[1]))


async def _seed(db, root: str, mode: str, max_files: int):
    from app.models.connection import Connection
    from app.models.data_source import DataSource
    from app.models.domain_connection import domain_connection
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.report_data_source_association import report_data_source_association
    from app.models.user import User

    suffix = uuid.uuid4().hex[:8]
    org = Organization(name=f"Bench Org {suffix}")
    db.add(org)
    await db.flush()
    user = User(name="Bench User", email=f"bench-{suffix}@example.com", hashed_password="x",
                is_active=True, is_superuser=False, is_verified=True)
    db.add(user)
    await db.flush()
    conn = Connection(
        organization_id=org.id, name=f"Bench Share {suffix}", type="network_dir",
        config={"root_path": root, "recursive": True, "index_mode": mode,
                "max_catalog_objects": max_files, "max_file_mb": 200},
        auth_policy="system_only",
    )
    conn.encrypt_credentials({})
    db.add(conn)
    await db.flush()
    ds = DataSource(name=f"Bench DS {suffix}", organization_id=org.id, is_active=True,
                    owner_user_id=user.id)
    db.add(ds)
    await db.flush()
    await db.execute(domain_connection.insert().values(data_source_id=ds.id, connection_id=conn.id))
    report = Report(title=f"Bench {suffix}", slug=f"bench-{suffix}", organization_id=org.id,
                    user_id=user.id)
    db.add(report)
    await db.flush()
    await db.execute(report_data_source_association.insert().values(
        report_id=report.id, data_source_id=ds.id))
    await db.commit()
    return org.id, user.id, conn.id, ds.id, report.id


async def _ctx(db, org_id, user_id, report_id):
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.models.data_source import DataSource
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User

    org = (await db.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
    report = (await db.execute(
        select(Report).where(Report.id == report_id)
        .options(selectinload(Report.data_sources).selectinload(DataSource.connections))
    )).scalar_one()
    return {"db": db, "organization": org, "user": user, "report": report}


async def _index_size(db, conn_id) -> dict:
    from sqlalchemy import text

    out = {}
    dialect = db.get_bind().dialect.name
    try:
        if dialect == "postgresql":
            for t in ("file_index_sections", "file_index_files", "connection_tables"):
                size = (await db.execute(
                    text("SELECT pg_total_relation_size(to_regclass(:t))"), {"t": t})).scalar_one()
                if size is not None:
                    out[t] = int(size)
        else:
            rows = (await db.execute(text(
                "SELECT name, SUM(pgsize) FROM dbstat WHERE name LIKE 'file_index%' "
                "OR name = 'connection_tables' GROUP BY name"))).all()
            out = {r[0]: int(r[1]) for r in rows}
    except Exception as e:  # size is informational
        await db.rollback()
        out = {"error": str(e)[:200]}
    return out


async def run(args) -> dict:
    from scripts.file_search_corpus import Corpus, build_corpus

    root = Path(args.corpus)
    manifest = root.parent / (root.name + ".manifest.json")
    if manifest.exists() and not args.rebuild:
        corpus = Corpus.load(manifest)
    else:
        t0 = time.perf_counter()
        corpus = build_corpus(root, n_files=args.files, seed=args.seed, per_kind=args.per_kind)
        corpus.save(manifest)
        print(f"corpus: {corpus.files} files, {corpus.bytes / 1e6:.1f} MB "
              f"in {time.perf_counter() - t0:.1f}s", flush=True)

    _migrate()
    import main  # noqa: F401  (registers every SQLAlchemy mapper)
    from app.ai.tools.implementations.search_files import SearchFilesTool
    from app.dependencies import async_session_maker
    from app.services.data_source_service import DataSourceService

    result = {"label": args.label or args.mode, "mode": args.mode,
              "files": corpus.files, "corpus_bytes": corpus.bytes}

    async with async_session_maker() as db:
        result["database"] = db.get_bind().dialect.name
        org_id, user_id, conn_id, ds_id, report_id = await _seed(
            db, str(root), args.mode, max(corpus.files + 1000, 10000))
        ctx = await _ctx(db, org_id, user_id, report_id)

        t0 = time.perf_counter()
        await DataSourceService().refresh_data_source_schema(
            db, str(ds_id), ctx["organization"], ctx["user"])
        result["index_seconds"] = round(time.perf_counter() - t0, 1)
        print(f"indexed in {result['index_seconds']}s", flush=True)

        if args.reindex:
            t0 = time.perf_counter()
            await DataSourceService().refresh_data_source_schema(
                db, str(ds_id), ctx["organization"], ctx["user"])
            result["reindex_unchanged_seconds"] = round(time.perf_counter() - t0, 1)
            print(f"re-indexed (unchanged) in {result['reindex_unchanged_seconds']}s", flush=True)

        result["index_bytes"] = await _index_size(db, conn_id)

        per_query = []
        tool = SearchFilesTool()
        for q in corpus.queries:
            ctx = await _ctx(db, org_id, user_id, report_id)
            t0 = time.perf_counter()
            last = None
            async for ev in tool.run_stream(
                    {"connection_id": str(conn_id), "query": q.query, "max_results": 10}, ctx):
                last = ev
            ms = (time.perf_counter() - t0) * 1000
            out = last.payload["output"]
            ids = [f.get("path") or f.get("id") for f in (out.get("files") or [])]
            rank = next((i + 1 for i, p in enumerate(ids) if p in q.expected), None)
            located = None
            if rank and q.location:
                hit = (out.get("files") or [])[rank - 1]
                matches = hit.get("matches") or []
                key = "page" if "page" in q.location else "sheet" if "sheet" in q.location else None
                if key:
                    located = any(m.get(key) == q.location[key] for m in matches)
            per_query.append({
                "id": q.id, "kind": q.kind, "query": q.query, "rank": rank,
                "ms": round(ms, 1), "source": out.get("source"), "index": out.get("index"),
                "returned": len(ids), "incomplete": bool(out.get("incomplete")),
                "located": located, "success": bool(out.get("success")),
            })
        result["queries"] = per_query

    result["summary"] = summarize(per_query)
    return result


def summarize(per_query) -> dict:
    def block(rows):
        n = len(rows)
        ranks = [r["rank"] for r in rows]
        ms = sorted(r["ms"] for r in rows)
        located = [r["located"] for r in rows if r["located"] is not None]
        return {
            "n": n,
            "top1": round(sum(1 for r in ranks if r == 1) / n, 3),
            "top5": round(sum(1 for r in ranks if r and r <= 5) / n, 3),
            "top10": round(sum(1 for r in ranks if r and r <= 10) / n, 3),
            "mrr": round(sum(1.0 / r for r in ranks if r) / n, 3),
            "p50_ms": round(statistics.median(ms), 1),
            "p95_ms": round(ms[min(n - 1, int(n * 0.95))], 1),
            "live_scans": sum(1 for r in rows if r["source"] in ("live", "catalog+live")),
            "located": (round(sum(1 for x in located if x) / len(located), 3) if located else None),
        }

    kinds = sorted({r["kind"] for r in per_query})
    return {"overall": block(per_query), **{k: block([r for r in per_query if r["kind"] == k]) for k in kinds}}


def print_summary(result: dict) -> None:
    s = result["summary"]
    print(f"\n== {result['label']} · {result.get('database')} · {result['files']} files · "
          f"index build {result.get('index_seconds')}s ==")
    print(f"{'kind':<12}{'n':>4}{'top1':>7}{'top5':>7}{'top10':>7}{'mrr':>7}{'p50ms':>9}{'p95ms':>9}{'live':>6}")
    for k, b in s.items():
        print(f"{k:<12}{b['n']:>4}{b['top1']:>7.0%}{b['top5']:>7.0%}{b['top10']:>7.0%}"
              f"{b['mrr']:>7.2f}{b['p50_ms']:>9.0f}{b['p95_ms']:>9.0f}{b['live_scans']:>6}")


def compare(a_path: str, b_path: str) -> None:
    a, b = json.loads(Path(a_path).read_text()), json.loads(Path(b_path).read_text())
    print(f"{'kind':<12}{'n':>4} | {a['label'] + ' top10':>20}{b['label'] + ' top10':>20} | "
          f"{'p50 ms':>10}{'p50 ms':>10}")
    for k in a["summary"]:
        x, y = a["summary"][k], b["summary"].get(k, {})
        print(f"{k:<12}{x['n']:>4} | {x['top10']:>20.0%}{y.get('top10', 0):>20.0%} | "
              f"{x['p50_ms']:>10.0f}{y.get('p50_ms', 0):>10.0f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", help="Directory for the generated file share")
    ap.add_argument("--files", type=int, default=2000)
    ap.add_argument("--per-kind", type=int, default=15)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--mode", default="fulltext", choices=["content", "fulltext", "metadata"])
    ap.add_argument("--label")
    ap.add_argument("--out")
    ap.add_argument("--rebuild", action="store_true", help="Regenerate the corpus even if present")
    ap.add_argument("--reindex", action="store_true", help="Also time a second, unchanged index run")
    ap.add_argument("--compare", nargs=2, metavar=("A.json", "B.json"))
    args = ap.parse_args()
    if args.compare:
        compare(*args.compare)
        return
    if not args.corpus:
        ap.error("--corpus is required")
    if not os.environ.get("BOW_DATABASE_URL"):
        ap.error("set BOW_DATABASE_URL")
    result = asyncio.run(run(args))
    print_summary(result)
    if args.out:
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
