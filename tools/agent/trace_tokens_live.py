#!/usr/bin/env python3
"""Loop B for docs/feedback-loops/trace-tokens-and-models.md (BOW-216).

Drives real agent runs on a running stack with a real LLM, then checks the
trace modal's data at the DB and HTTP layers:

  1. a multi-step chat on the demo data source: per-step tokens in the trace
     equal plan_decisions.metrics_json (input normalized: cache included), and
     the trace's model equals agent_executions.primary_model_id;
  2. a training conversation: a broad bow.runs query leaves the conversation
     out, and a query naming it (report_id:<this report>) returns its turns
     with per-turn tokens;
  3. the diagnosis llm:<model> filter returns those runs.

    cd backend && uv run python ../tools/agent/trace_tokens_live.py [--db-path db/agent.db]

Needs an org with an LLM configured (setup_haiku_llm.py) and the demo data
source (seed_org.py --demo). Prints report ids for the UI captures.
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import httpx

p = argparse.ArgumentParser()
p.add_argument("--base-url", default=os.environ.get("BOW_BASE_URL", "http://localhost:8000"))
p.add_argument("--email", default=os.environ.get("BOW_ADMIN_EMAIL", "admin@example.com"))
p.add_argument("--password", default=os.environ.get("BOW_ADMIN_PASSWORD", "Password123!"))
p.add_argument("--db-path", default="db/agent.db")
a = p.parse_args()

c = httpx.Client(base_url=a.base_url, timeout=600)
tok = c.post("/api/auth/jwt/login", data={"username": a.email, "password": a.password}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}
failures = []


def ok(r, what):
    if r.status_code >= 400:
        sys.exit(f"{what}: HTTP {r.status_code} {r.text[:300]}")
    return r.json()


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def ask(report_id, prompt, mode="chat"):
    ok(c.post(f"/api/reports/{report_id}/completions", headers=H, params={"background": False},
              json={"prompt": {"content": prompt, "widget_id": None, "step_id": None, "mentions": [{}], "mode": mode}}),
       "completion")


def runs_of(report_id):
    db = sqlite3.connect(a.db_path)
    try:
        return db.execute("select id, primary_model_id, primary_provider from agent_executions "
                          "where report_id=? order by created_at", (report_id,)).fetchall()
    finally:
        db.close()


def trace(run_id):
    return ok(c.get(f"/api/console/agent_executions/{run_id}", headers=H), "trace")


# 1. Multi-step chat -------------------------------------------------------------
ds = ok(c.get("/api/data_sources/active", headers=H), "data sources")
chat = ok(c.post("/api/reports", headers=H, json={"title": "Revenue by genre", "files": [],
                                                  "data_sources": [ds[0]["id"]] if ds else []}), "report")
ask(chat["id"], "Which 5 genres bring in the most revenue? Look at the tables first, then show a bar chart.")
chat_runs = runs_of(chat["id"])
check(len(chat_runs) == 1, f"chat produced one run ({len(chat_runs)})")
run_id, primary_model, primary_provider = chat_runs[0]
body = trace(run_id)
db = sqlite3.connect(a.db_path)
decisions = {r[0]: json.loads(r[1] or "{}") for r in db.execute(
    "select id, metrics_json from plan_decisions where agent_execution_id=?", (run_id,))}
db.close()
seen, steps = set(), []
for b in body["completion_blocks"]:
    pd = b.get("plan_decision")
    if pd and pd["id"] not in seen and (pd.get("metrics_json") or {}).get("token_usage"):
        seen.add(pd["id"])
        steps.append((pd["id"], pd["metrics_json"]["token_usage"]))
check(len(steps) >= 2, f"multi-step run: {len(steps)} planner steps with tokens")
for pd_id, tu in steps:
    raw = decisions[pd_id]["token_usage"]
    expected_in = raw["prompt_tokens"] + (raw.get("cache_read_tokens") or 0) + (raw.get("cache_creation_tokens") or 0)
    check(tu["input_tokens"] == expected_in and tu["completion_tokens"] == raw["completion_tokens"],
          f"step {pd_id[:8]}: in {tu['input_tokens']} == prompt {raw['prompt_tokens']} + cache "
          f"{(raw.get('cache_read_tokens') or 0) + (raw.get('cache_creation_tokens') or 0)}, out {tu['completion_tokens']}")
ae = body["agent_execution"]
check(ae["primary_model_id"] == primary_model and primary_model,
      f"trace model {ae['primary_model_id']} == agent_executions.primary_model_id {primary_model}")
check(any(m["model_id"] == primary_model for m in body["models"]),
      f"models breakdown: {[(m['model_id'], m['calls']) for m in body['models']]}")

# 2. Training conversation reads its own runs ------------------------------------------
training = ok(c.post("/api/reports", headers=H, json={"title": "Token review", "files": [], "mode": "training"}), "training")
ok(c.put(f"/api/reports/{training['id']}", headers=H, json={"mode": "training"}), "training mode")
ask(training["id"], "How many agent runs were there in the last 7 days? Use bow.runs, one count.", mode="training")
ask(training["id"], "Now show the input and output tokens for each turn of this conversation from bow.runs.", mode="training")


def bow_queries(report_id):
    db = sqlite3.connect(a.db_path)
    try:
        rows = db.execute(
            "select s.code from tool_executions te join agent_executions ae on ae.id=te.agent_execution_id "
            "join steps s on s.id=te.created_step_id where ae.report_id=? and te.tool_name='create_data' "
            "order by te.created_at", (report_id,)).fetchall()
        return [r[0] or "" for r in rows]
    finally:
        db.close()


codes = bow_queries(training["id"])
print("training create_data code:\n" + "\n---\n".join(codes))
check(any(f"report_id:{training['id']}" in code for code in codes),
      "the agent named this conversation (report_id:<current>) in a bow.runs query")
check(any("bow" in code and f"report_id:{training['id']}" not in code for code in codes),
      "the broad query did not name it")

# The same two queries, run from this conversation as saved queries (real path).
def saved_rows(query):
    q = ok(c.post("/api/queries", headers=H, json={"title": "bow check", "report_id": training["id"]}), "query")
    code = ("def generate_df(ds_clients, excel_files):\n"
            f"    return ds_clients['bow'].execute_query({{'dataset':'runs','query':{query!r},"
            "'time_range':{'relative':'1d'},'columns':['run_id','report_id','turn','input_tokens','output_tokens']})\n")
    ran = ok(c.post(f"/api/queries/{q['id']}/run", headers=H, json={"code": code}), "run query")
    if ran.get("error"):
        sys.exit(f"bow query failed: {ran['error']}")
    return ok(c.get(f"/api/queries/{q['id']}", headers=H), "saved")["default_step"]["data"]["rows"]


broad = saved_rows("")
named = saved_rows(f"report_id:{training['id']}")
training_runs = {r[0] for r in runs_of(training["id"])}
check(not ({r["run_id"] for r in broad} & training_runs), f"broad query: {len(broad)} rows, none from this conversation")
check({r["run_id"] for r in named} == training_runs,
      f"report_id:<current>: {[(r['turn'], r['input_tokens'], r['output_tokens']) for r in named]}")

# 3. Diagnosis llm: filter -----------------------------------------------------------------
now = datetime.now(timezone.utc)
diag = ok(c.get("/api/console/diagnosis/runs", headers=H, params={
    "q": f"llm:{primary_model}", "start": (now - timedelta(days=1)).isoformat(), "end": (now + timedelta(hours=1)).isoformat()}),
    "diagnosis")
found = {i["id"] for i in diag["items"]}
check({run_id} | training_runs <= found, f"llm:{primary_model} returns the chat and training runs ({len(found)} runs)")

print(json.dumps({"chat_report": chat["id"], "chat_run": run_id, "training_report": training["id"],
                  "model": primary_model, "provider": primary_provider}, indent=2))
sys.exit(1 if failures else 0)
