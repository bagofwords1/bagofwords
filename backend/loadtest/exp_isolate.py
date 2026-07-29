#!/usr/bin/env python3
"""
Experiment: what actually slows the agent flow down under load?

The mixed-workload ramp shows the agent flow degrading from ~10s solo to ~60s
at L100, and it shows ordinary browsing degrading 30x. Both happen at once, so
the ramp alone cannot say which causes which. Query profiling points at the
org-wide `instruction_builds` row (a single-row SELECT averaging 1.8s = lock
wait, and the top queries by total DB time are all on that table), which is
written by *cohort B*, not by the agents.

So: run the same agent concurrency three ways and compare.

  agents_only   — N agents, zero browsing traffic
  agents_browse — N agents + the usual browsing cohort (the ramp's mix)
  browse_only   — browsing traffic alone, for reference

If agents_only is fast and agents_browse is slow, the agent flow is not
self-limiting at this scale — it is collateral damage from instruction writes
serializing on a shared row. If both are slow, the agent flow limits itself.
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def snapshot_queries(out):
    """Top queries by total DB time for this arm, so the two arms can be
    compared on *what* the database was busy with, not just how slow it was."""
    import sqlalchemy as sa
    url = os.getenv("BOW_DATABASE_URL",
                    "postgresql://bow:bow@127.0.0.1:5432/bow")
    eng = sa.create_engine(url)
    try:
        with eng.begin() as c:
            rows = c.execute(sa.text("""
                SELECT round(total_exec_time::numeric) AS total_ms, calls,
                       round(mean_exec_time::numeric, 2) AS mean_ms,
                       left(regexp_replace(query, '\s+', ' ', 'g'), 130) AS q
                FROM pg_stat_statements
                WHERE query NOT LIKE '%pg_stat_statements%'
                ORDER BY total_exec_time DESC LIMIT 15""")).fetchall()
        with open(out, "w") as fh:
            for r in rows:
                fh.write(f"{r[0]}|{r[1]}|{r[2]}|{r[3]}\n")
    except Exception as e:
        print("query snapshot failed:", e)
    finally:
        eng.dispose()


def reset_queries():
    import sqlalchemy as sa
    url = os.getenv("BOW_DATABASE_URL",
                    "postgresql://bow:bow@127.0.0.1:5432/bow")
    eng = sa.create_engine(url)
    try:
        with eng.begin() as c:
            c.execute(sa.text("SELECT pg_stat_statements_reset()"))
    except Exception:
        pass
    finally:
        eng.dispose()


def run(level, agent_frac, out, timeout=600, baseline_only=False):
    reset_queries()
    cmd = [sys.executable, os.path.join(HERE, "harness_v2.py"),
           "--level", str(level), "--agent-frac", str(agent_frac),
           "--timeout", str(timeout), "--out", out]
    if baseline_only:
        cmd += ["--baseline-only", "--duration", "90"]
    subprocess.run(cmd, cwd=os.path.dirname(HERE), check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    snapshot_queries(out.replace(".json", "_queries.txt"))
    try:
        return json.load(open(out))["summary"]
    except Exception:
        return {}


def show(tag, s):
    if not s:
        print(f"{tag:16s} (no result)")
        return
    a, b = s.get("agent", {}), s.get("browse", {})
    ss = s.get("server_side", {}) or {}
    by = ss.get("by_status", {})
    print(f"{tag:16s} agents={s.get('n_agent'):>3}  "
          f"client_ok={a.get('success_pct')}%  "
          f"db={by}  "
          f"p50={a.get('t_total_p50') and round(a['t_total_p50'],1)}s  "
          f"p95={a.get('t_total_p95') and round(a['t_total_p95'],1)}s  "
          f"browse_p95={b.get('p95') and round(b['p95']*1000)}ms  "
          f"browse_fail={b.get('fail')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", type=int, default=36,
                    help="agent count held constant across arms")
    ap.add_argument("--outdir", default=os.path.join(HERE, "results", "isolate"))
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    n = args.agents

    print(f"holding agent concurrency at {n} across arms\n")
    results = {}

    # agents only: level == n, all of it agents
    s = run(n, 1.0, os.path.join(args.outdir, "agents_only.json"))
    results["agents_only"] = s
    show("agents_only", s)
    import time
    time.sleep(25)

    # agents + browse: same n agents, plus ~0.67n browsers (the ramp's ratio)
    lvl = int(round(n / 0.6))
    s = run(lvl, 0.6, os.path.join(args.outdir, "agents_browse.json"))
    results["agents_browse"] = s
    show("agents_browse", s)
    time.sleep(25)

    # browse only
    s = run(lvl, 0.6, os.path.join(args.outdir, "browse_only.json"),
            baseline_only=True)
    results["browse_only"] = s
    show("browse_only", s)

    json.dump(results, open(os.path.join(args.outdir, "summary.json"), "w"),
              indent=2, default=str)
    print(f"\nwrote {args.outdir}/summary.json")


if __name__ == "__main__":
    main()
