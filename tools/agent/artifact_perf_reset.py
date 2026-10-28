#!/usr/bin/env python3
"""Reset per-viewer state for the artifact perf loop's seeded reports.

Run before EACH harness run (tools/agent/artifact_perf_harness.mjs) so a
before/after comparison starts from identical server state. Two things in the
app depend on history, not code:

  * step_user_results — a member's parameter run is cached per viewer, and
    their next load serves that slice;
  * refresh-on-view — an owner visit reruns the report at most once per
    REFRESH_ON_VIEW_MIN_INTERVAL_SECONDS (wall-clock bucketed claim in
    scheduled_job_runs + reports.last_run_at), and a rerun deletes viewers'
    cached slices.

    python3 tools/agent/artifact_perf_reset.py <seed.json> [backend/db/agent.db]

Sandbox-only: direct writes to the agent's throwaway SQLite database.
"""
import json
import sqlite3
import sys

seed = json.load(open(sys.argv[1]))
db = sys.argv[2] if len(sys.argv) > 2 else "backend/db/agent.db"
conn = sqlite3.connect(db)
totals = [0, 0, 0]
for info in seed.values():
    rid = info["report_id"]
    totals[0] += conn.execute("delete from step_user_results where report_id=?", (rid,)).rowcount
    totals[1] += conn.execute("delete from scheduled_job_runs where job_id=?", (f"report_view_{rid}",)).rowcount
    totals[2] += conn.execute("update reports set last_run_at=NULL where id=?", (rid,)).rowcount
conn.commit()
print(f"reset: {totals[0]} viewer results, {totals[1]} refresh claims, {totals[2]} reports")
