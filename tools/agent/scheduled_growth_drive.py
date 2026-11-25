#!/usr/bin/env python3
"""Drive N runs of a scheduled prompt through the real API and report how big
each run's planner prompts were (as recorded by scheduled_growth_stub_llm.py).

    cd backend && uv run python ../tools/agent/scheduled_growth_drive.py \
        --runs 30 --mode host --source-id <ds-id>

--mode host   : spawn_new_report=False (the customer's setup: same report)
--mode spawn  : spawn_new_report=True  (fresh report per run, the baseline)

Writes run markers into the stub log, waits for each run's final planner
round, then prints one row per run: planner calls, max / total estimated
input tokens, and the run's total estimated input tokens (all LLM calls).
"""
import argparse
import json
import os
import time

import httpx

p = argparse.ArgumentParser()
p.add_argument("--base-url", default="http://localhost:8000")
p.add_argument("--email", default=os.environ.get("BOW_EMAIL", "admin@example.com"))
p.add_argument("--password", default=os.environ.get("BOW_PASSWORD", "Password123!"))
p.add_argument("--source-id", required=True)
p.add_argument("--runs", type=int, default=20)
p.add_argument("--mode", choices=["host", "spawn"], default="host")
p.add_argument("--log", default=os.environ.get("STUB_LOG", "/tmp/sched_growth_stub.jsonl"))
p.add_argument("--out", default=None, help="write per-run rows as JSON here")
p.add_argument("--timeout", type=int, default=240)
a = p.parse_args()

c = httpx.Client(base_url=a.base_url, timeout=120, trust_env=False)
tok = c.post("/api/auth/jwt/login", data={"username": a.email, "password": a.password}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}

rep = c.post("/api/reports", json={"title": f"Daily revenue ({a.mode})", "files": [],
                                   "data_sources": [a.source_id]}, headers=H)
rep.raise_for_status()
report_id = rep.json()["id"]
sp = c.post(f"/api/reports/{report_id}/scheduled-prompts", json={
    "prompt": {"content": "Refresh the daily revenue dashboard with today's numbers "
                          "(orders by region, revenue by region) and comment on what changed."},
    "cron_schedule": "0 9 * * *", "spawn_new_report": a.mode == "spawn",
}, headers=H)
sp.raise_for_status()
sp_id = sp.json()["id"]
print(f"report={report_id} scheduled_prompt={sp_id} mode={a.mode}", flush=True)


def read_log():
    if not os.path.exists(a.log):
        return []
    with open(a.log) as f:
        return [json.loads(line) for line in f if line.strip()]


rows = []
for run in range(1, a.runs + 1):
    with open(a.log, "a") as f:
        f.write(json.dumps({"marker": "run_start", "run": run, "mode": a.mode, "ts": time.time()}) + "\n")
    start_len = len(read_log())
    c.post(f"/api/reports/{report_id}/scheduled-prompts/{sp_id}/trigger", headers=H).raise_for_status()
    deadline = time.time() + a.timeout
    done = False
    while time.time() < deadline:
        recs = read_log()[start_len:]
        if any(r.get("kind") == "planner" and r.get("round") == 3 for r in recs):
            done = True
            break
        time.sleep(1)
    time.sleep(3)  # let post-answer calls (titles, judges) land in this run's window
    recs = [r for r in read_log()[start_len:] if "kind" in r]
    planner = [r["est_tokens"] for r in recs if r["kind"] == "planner"]
    conv = [r.get("conversation_tokens", 0) for r in recs if r["kind"] == "planner"]
    base = [r.get("baseline_tokens", 0) for r in recs if r["kind"] == "planner"]
    row = {
        "run": run, "finished": done,
        "planner_calls": len(planner),
        "planner_max_tokens": max(planner, default=0),
        "planner_max_conversation_tokens": max(conv, default=0),
        "planner_baseline_tokens": max(base, default=0),
        "planner_total_tokens": sum(planner),
        "all_calls": len(recs),
        "all_total_tokens": sum(r["est_tokens"] for r in recs),
    }
    rows.append(row)
    print(json.dumps(row), flush=True)

if a.out:
    with open(a.out, "w") as f:
        json.dump({"mode": a.mode, "report_id": report_id, "scheduled_prompt_id": sp_id, "rows": rows}, f, indent=1)
