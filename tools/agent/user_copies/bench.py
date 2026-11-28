#!/usr/bin/env python3
"""Measure what one user's cached copy costs to build, hold and query.

Uses the product's own extraction (`extractor.extract_to_artifact`) and its
own DuckDB serving client (`FastQueryClient`) against a Postgres table, so the
numbers are the ones a real copy would show. Each measurement runs in a
process of its own, because peak memory is only meaningful for a fresh one.

    cd backend && uv run python ../tools/agent/user_copies/bench.py sweep
    cd backend && uv run python ../tools/agent/user_copies/bench.py concurrent 8 1000000

`sweep` builds a copy at each size in UC_BENCH_SIZES and times a fixed set of
questions on it. `concurrent N ROWS` builds N copies of ROWS rows at once, the
way N people asking their first question at the same moment would.

Env: UC_PG_HOST/PORT/DB/ADMIN_USER/ADMIN_PASSWORD (the source),
UC_BENCH_TABLE (sales_big), UC_BENCH_SIZES (comma-separated row counts).
Prints one JSON object per line.
"""
import json
import os
import resource
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

TABLE = os.environ.get("UC_BENCH_TABLE", "sales_big")
SIZES = [int(x) for x in os.environ.get(
    "UC_BENCH_SIZES", "100000,1000000,5000000,10000000,20000000,30000000").split(",")]

# What an analyst's questions look like once the copy is local.
QUESTIONS = {
    "count": "SELECT count(*) FROM t",
    "sum_by_department": "SELECT department_id, sum(amount), count(*) FROM t GROUP BY 1 ORDER BY 1",
    "top_customers": "SELECT customer, sum(amount) s FROM t GROUP BY 1 ORDER BY s DESC LIMIT 10",
    "month_trend": "SELECT date_trunc('month', sold_at) m, sum(amount) FROM t GROUP BY 1 ORDER BY 1",
    "filtered_range": "SELECT count(*), avg(amount) FROM t WHERE sold_at >= DATE '2025-06-01' "
                      "AND sold_at < DATE '2025-09-01' AND company_id = 1",
    "distinct_customers": "SELECT count(DISTINCT customer) FROM t",
    "self_join": "SELECT count(*) FROM t a JOIN (SELECT customer, max(amount) m FROM t GROUP BY 1) b "
                 "ON a.customer = b.customer AND a.amount = b.m",
    "text_search": "SELECT count(*) FROM t WHERE note LIKE '%abc%'",
}


def _client():
    from app.data_sources.clients.postgresql_client import PostgresqlClient

    return PostgresqlClient(
        host=os.environ.get("UC_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("UC_PG_PORT", "55432")),
        database=os.environ.get("UC_PG_DB", "rlsdemo"),
        user=os.environ.get("UC_PG_ADMIN_USER", "postgres"),
        password=os.environ["UC_PG_ADMIN_PASSWORD"],
    )


def _peak_mb() -> float:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def _build(rows: int, root: str, name: str = "t"):
    from unittest.mock import patch

    from app.data_sources.fast import artifacts, extractor

    with patch.object(artifacts, "_ARTIFACT_ROOT", Path(root)):
        started = time.monotonic()
        res = extractor.extract_to_artifact(
            _client(), f"SELECT * FROM {TABLE} WHERE id <= {rows}", name, "bench",
            max_rows=10**9, max_bytes=10**13, max_seconds=7200)
        return res, round(time.monotonic() - started, 2)


def one(rows: int) -> dict:
    from app.data_sources.fast.fast_client import FastQueryClient, FastRelation

    with tempfile.TemporaryDirectory(prefix="uc-bench-") as root:
        res, build_s = _build(rows, root)
        out = {
            "rows": res.row_count, "build_s": build_s,
            "rows_per_s": int(res.row_count / build_s) if build_s else None,
            "file_mb": round(res.artifact_bytes / 1e6, 1),
            "bytes_per_row": round(res.artifact_bytes / max(1, res.row_count), 1),
            "build_peak_rss_mb": _peak_mb(),
        }
        client = FastQueryClient([FastRelation(
            name="t", artifact_path=res.artifact_path, artifact_key=res.artifact_key,
            columns=res.columns, row_count=res.row_count)])
        timings = {}
        for label, sql in QUESTIONS.items():
            best = None
            for _ in range(3):
                t0 = time.monotonic()
                client.execute_query(sql)
                took = time.monotonic() - t0
                best = took if best is None else min(best, took)
            timings[label] = round(best * 1000, 1)
        out["query_ms"] = timings
        out["query_peak_rss_mb"] = _peak_mb()
        return out


def concurrent(n: int, rows: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="uc-bench-") as root:
        results, errors = [], []

        def work(i: int) -> None:
            try:
                res, took = _build(rows, root, name=f"t{i}")
                results.append((took, res.row_count, res.artifact_bytes))
            except Exception as e:  # reported, not hidden
                errors.append(f"{type(e).__name__}: {e}"[:200])

        started = time.monotonic()
        threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall = round(time.monotonic() - started, 2)
        times = sorted(t for t, _r, _b in results)
        return {
            "builds_at_once": n, "rows_each": rows, "ok": len(results), "errors": errors[:3],
            "wall_s": wall, "fastest_s": times[0] if times else None,
            "slowest_s": times[-1] if times else None,
            "total_rows_per_s": int(sum(r for _t, r, _b in results) / wall) if wall else None,
            "disk_mb": round(sum(b for _t, _r, b in results) / 1e6, 1),
            "peak_rss_mb": _peak_mb(),
        }


def _child(*args: str) -> None:
    proc = subprocess.run([sys.executable, __file__, *args], capture_output=True, text=True)
    line = next((ln for ln in reversed(proc.stdout.splitlines()) if ln.startswith("{")), None)
    print(line or json.dumps({"args": args, "failed": (proc.stderr or proc.stdout)[-400:]}), flush=True)


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "sweep":
        for size in SIZES:
            _child("one", str(size))
    elif mode == "one":
        import main  # noqa: F401
        print(json.dumps(one(int(sys.argv[2]))))
    elif mode == "concurrent":
        if len(sys.argv) > 4 and sys.argv[4] == "run":
            import main  # noqa: F401
            print(json.dumps(concurrent(int(sys.argv[2]), int(sys.argv[3]))))
        else:
            _child("concurrent", sys.argv[2], sys.argv[3], "run")
    else:
        sys.exit(__doc__)
