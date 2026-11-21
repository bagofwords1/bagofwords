#!/usr/bin/env python3
"""Live loop: an MCP agent's tools must be callable beside a SQL agent.

Reproduces "MCP agents are dropped in multi-agent runs": an MCP agent whose
connection carries the cached unhealthy flag (Connection.is_active = False)
worked when it was the only agent on a report, and lost all its tools when a
SQL agent shared the report.

Needs, already running:
  * the backend (tools/agent/boot_stack.sh, or `python main.py`), seeded with
    tools/agent/seed_org.py;
  * mcp_cost_server.py  (real MCP server, streamable HTTP);
  * stub_llm_mcp_call.py, registered as the org's default model with
    setup_stub_llm.py.

    cd backend && uv run python ../tools/agent/mcp_beside_sql_loop.py \
        --db db/agent.db --mcp-url http://127.0.0.1:9310/mcp

Prints one line per case and exits non-zero when any case fails.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import uuid

import httpx

p = argparse.ArgumentParser()
p.add_argument("--base-url", default="http://localhost:8000")
p.add_argument("--email", default=os.environ.get("BOW_EMAIL", "admin@example.com"))
p.add_argument("--password", default=os.environ.get("BOW_PASSWORD", "Password123!"))
p.add_argument("--db", default="db/agent.db", help="the backend's SQLite file (for the health-flag write)")
p.add_argument("--mcp-url", default="http://127.0.0.1:9310/mcp")
p.add_argument("--mcp-log", default="/tmp/bow-agent/mcp_cost_calls.jsonl")
p.add_argument("--stub-log", default="/tmp/bow-agent/stub_llm_mcp_call.jsonl")
a = p.parse_args()

c = httpx.Client(base_url=a.base_url, timeout=180, trust_env=False)
tok = c.post("/api/auth/jwt/login", data={"username": a.email, "password": a.password}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}


def ok(r):
    if r.status_code >= 400:
        sys.exit(f"{r.request.method} {r.request.url} -> {r.status_code} {r.text[:400]}")
    return r.json()


# SQL agent: a small SQLite file, queried through a real query client.
db_path = os.path.join(tempfile.mkdtemp(), "sales.sqlite")
with sqlite3.connect(db_path) as s:
    s.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL)")
    s.execute("INSERT INTO orders (amount) VALUES (10), (20)")
sql_agent = ok(c.post("/api/data_sources", headers=H, json={
    "name": f"sales {uuid.uuid4().hex[:4]}", "type": "sqlite", "config": {"database": db_path},
    "credentials": {}, "auth_policy": "system_only", "generate_summary": False,
    "generate_conversation_starters": False, "generate_ai_rules": False}))["id"]

# MCP agent over the real MCP server.
slug = f"costs{uuid.uuid4().hex[:4]}"
conn = ok(c.post("/api/connections", headers=H, json={
    "name": slug, "type": "mcp", "auth_policy": "system_only", "credentials": {},
    "config": {"server_url": a.mcp_url, "transport": "streamable_http"}}))
for t in ok(c.post(f"/api/connections/{conn['id']}/refresh-tools", headers=H)):
    # A new MCP tool defaults to "ask" (a human approves each call); allow it
    # so the run is unattended. The policy is not what this loop tests.
    ok(c.put(f"/api/connections/{conn['id']}/tools/{t['id']}", headers=H, json={"policy": "allow"}))
mcp_agent = ok(c.post("/api/data_sources", headers=H, json={
    "name": f"{slug} agent", "connection_ids": [conn["id"]], "kind": "agent", "is_public": True,
    "generate_summary": False, "generate_conversation_starters": False,
    "generate_ai_rules": False, "use_llm_sync": False}))["id"]

# Direct write: Connection.is_active is the cached health flag a run of failed
# background probes sets; the API has no way to set it on demand. The agent's
# own flag stays True, as it does for agents whose connections are not all
# system_only, so the agent is still live and selectable.
with sqlite3.connect(a.db) as s:
    s.execute("UPDATE connections SET is_active = 0 WHERE id = ?", (conn["id"],))

tool = f"mcp__{slug}__cost_report"
failed = False
for label, agents in (("MCP agent alone", [mcp_agent]), ("MCP agent + SQL agent", [mcp_agent, sql_agent])):
    open(a.stub_log, "w").close()
    open(a.mcp_log, "a").close()
    calls_before = sum(1 for _ in open(a.mcp_log))
    rep = ok(c.post("/api/reports", headers=H, json={"title": label, "files": [], "data_sources": agents}))
    ok(c.post(f"/api/reports/{rep['id']}/completions", headers=H, params={"background": False},
              json={"prompt": {"content": "Pull the cost report for the last 30 days."}}))
    requests = [json.loads(line) for line in open(a.stub_log) if line.strip()]
    offered = bool(requests) and tool in requests[0]["tools"]
    ran = sum(1 for _ in open(a.mcp_log)) - calls_before
    answered = len(requests) > 1 and requests[1]["tool_results"] > 0
    passed = offered and ran > 0 and answered
    failed |= not passed
    print(f"{'PASS' if passed else 'FAIL'}  {label:24s} tool offered={offered}  MCP server calls={ran}  "
          f"planner saw result={answered}")

sys.exit(1 if failed else 0)
