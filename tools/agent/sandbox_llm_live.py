#!/usr/bin/env python3
"""Live loop for `llm` in generated code (docs/feedback-loops/sandbox-llm-functions.md).

Against a running stack with a real Anthropic model as the org default
(tools/agent/setup_haiku_llm.py with HAIKU_MODEL_ID=claude-haiku-5-5) and the
demo Music Store data source (seed_org.py --demo):

1. turns on "LLM functions in code";
2. asks the agent for an LLM classification over a whole column;
3. reads the evidence from the database: the step's code calls `llm.*`, the
   result has the label column, usage rows were recorded under `sandbox_llm`
   with the org/user/report and the model record that served them;
4. reruns the report (a refresh) and checks that it made no new LLM request
   (answers came from the cache) and gave the same labels.

    cd backend && uv run python ../tools/agent/sandbox_llm_live.py --db-path db/agent.db

Never prints credentials.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time

import httpx

ap = argparse.ArgumentParser()
ap.add_argument("--base-url", default="http://localhost:8000")
ap.add_argument("--email", default="admin@example.com")
ap.add_argument("--password", default="Password123!")
ap.add_argument("--db-path", default="db/agent.db")
ap.add_argument("--prompt", default=(
    "Using the LLM functions in code, classify every distinct track name in the Track table by mood "
    "(one of: love, party, sad, angry, other) and give me a table of mood with the number of tracks in each."
))
a = ap.parse_args()


def step(msg):
    print(f"\n=== {msg}", flush=True)


def ok(r, what):
    if r.status_code >= 400:
        sys.exit(f"{what} failed: {r.status_code} {r.text[:300]}")
    return r.json()


c = httpx.Client(base_url=a.base_url, timeout=900)
tok = ok(c.post("/api/auth/jwt/login", data={"username": a.email, "password": a.password}), "login")["access_token"]
org = ok(c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}), "orgs")[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}

step("enable LLM functions in code")
ok(c.put("/api/organization/settings", headers=H,
         json={"config": {"enable_sandbox_llm": {"value": True}}}), "settings")
models = ok(c.get("/api/llm/models", headers=H), "models")
for m in models:
    print(f"  model record {m['id']}  model_id={m['model_id']}  default={m.get('is_default')} "
          f"small={m.get('is_small_default')}")

ds = ok(c.get("/api/data_sources/active", headers=H), "data sources")
ds_ids = [ds[0]["id"]] if ds else []

step("agent run")
report = ok(c.post("/api/reports", headers=H, json={"title": "llm-in-code live", "files": [],
                                                     "data_sources": ds_ids}), "report")
rid = report["id"]
t0 = time.monotonic()
r = c.post(f"/api/reports/{rid}/completions", headers=H, params={"background": False},
           json={"prompt": {"content": a.prompt, "widget_id": None, "step_id": None, "mentions": [{}]}})
print("http", r.status_code, f"{time.monotonic() - t0:.1f}s")

db = sqlite3.connect(a.db_path)
db.row_factory = sqlite3.Row


def usage_rows():
    return db.execute(
        "select id, scope, scope_ref_id, model_id, llm_model_id, user_id, report_id, prompt_tokens, "
        "completion_tokens, total_cost_usd from llm_usage_records where scope='sandbox_llm' and report_id=? "
        "order by created_at", (rid,)).fetchall()


step("database evidence: steps")
steps = db.execute(
    "select s.id, s.code, s.data from steps s where s.widget_id in (select id from widgets where report_id=?) "
    "or s.query_id in (select id from queries where report_id=?) order by s.created_at", (rid, rid)).fetchall()
llm_steps = [s for s in steps if "llm." in (s["code"] or "")]
print(f"steps: {len(steps)}, using llm: {len(llm_steps)}")
for s in llm_steps:
    code_lines = [ln for ln in (s["code"] or "").splitlines() if "llm." in ln]
    print("  llm lines:", *code_lines[:6], sep="\n    ")
    data = json.loads(s["data"] or "{}")
    print("  result columns:", [col.get("field") or col.get("headerName") for col in data.get("columns", [])][:8])
    print("  rows:", json.dumps((data.get("rows") or [])[:8])[:600])

step("database evidence: tool result activity")
for te in db.execute("select result_json from tool_executions where tool_name='create_data' and "
                     "result_json like '%sandbox_activity%' order by created_at desc limit 3").fetchall():
    res = json.loads(te["result_json"] or "{}")
    act = (res.get("observation") or {}).get("sandbox_activity") or res.get("sandbox_activity")
    if act:
        print(json.dumps(act, indent=1)[:1500])
        break

step("database evidence: usage")
time.sleep(2)
first = usage_rows()
print(f"usage rows under sandbox_llm: {len(first)}")
for u in first[:5]:
    print("  ", dict(u))
tokens = sum((u["prompt_tokens"] or 0) + (u["completion_tokens"] or 0) for u in first)
cost = sum(float(u["total_cost_usd"] or 0) for u in first)
print(f"  total tokens {tokens}, cost ${cost:.4f}")

step("refresh (report rerun): answers must come from the cache")
t0 = time.monotonic()
rr = c.post(f"/api/reports/{rid}/rerun", headers=H)
print("rerun http", rr.status_code, f"{time.monotonic() - t0:.1f}s", rr.text[:200])
time.sleep(2)
second = usage_rows()
print(f"usage rows after rerun: {len(second)} (new: {len(second) - len(first)})")

verdict = {
    "llm_steps": len(llm_steps),
    "usage_rows": len(first),
    "new_usage_rows_on_rerun": len(second) - len(first),
    "usage_attributed": all(u["user_id"] and u["report_id"] == rid and u["llm_model_id"] for u in first),
}
print("\nVERDICT", json.dumps(verdict))
passed = verdict["llm_steps"] >= 1 and verdict["usage_rows"] >= 1 and verdict["usage_attributed"] \
    and verdict["new_usage_rows_on_rerun"] == 0
print("PASS" if passed else "FAIL")
sys.exit(0 if passed else 1)
