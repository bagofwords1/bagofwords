"""Loop C driver: seed a running stack and ask an agent a routine-dependent question.

Prereqs: tools/agent/boot_stack.sh (with a stable BOW_ENCRYPTION_KEY exported),
`uv run python ../tools/agent/seed_org.py`, the two databases from
seed_pg.sql / seed_mssql.sql, and ANTHROPIC_API_KEY in the environment
(never committed, never printed).

    cd backend
    uv run python ../docs/feedback-loops/routines-in-schema/drive.py seed
    uv run python ../docs/feedback-loops/routines-in-schema/drive.py ask pg "What was our net revenue in fiscal Q1 of FY2026?"
    uv run python ../docs/feedback-loops/routines-in-schema/drive.py ask mssql "Run usp_load_net_revenue for February through April 2026 and tell me the total net revenue."

`ask` prints the tool calls the agent made and its final answer. The seeded
data makes the answer discriminating: net revenue for FY2026-Q1 (Feb–Apr,
complete orders, no test customers, refunds subtracted) is 4,050; a naive sum
of gross is 12,500 and a calendar-quarter guess is 2,600.
"""
import json
import os
import pathlib
import sys
import time

import httpx

BASE = os.environ.get("BOW_BASE", "http://localhost:8000")
STATE = pathlib.Path(os.environ.get("BOW_LOOP_STATE", "/tmp/routines-loop.json"))
AGENTS = {
    "pg": {"name": "Sales (Postgres)", "type": "postgresql",
           "config": {"host": "localhost", "port": int(os.environ.get("PG_PORT", 5433)),
                      "database": "salesdb", "schema": "sales"},
           "credentials": {"user": "postgres", "password": ""}},
    "mssql": {"name": "Sales (SQL Server)", "type": "MSSQL",
              "config": {"host": "localhost", "port": 1433, "database": "salesdb", "encrypt": False},
              "credentials": {"user": "sa", "password": os.environ.get("MSSQL_SA_PASSWORD", "Sandbox!Pass123")}},
    # Can read data, cannot see routine source (no VIEW DEFINITION).
    "reader": {"name": "Sales (SQL Server, reader)", "type": "MSSQL",
               "config": {"host": "localhost", "port": 1433, "database": "salesdb", "encrypt": False},
               "credentials": {"user": "reader", "password": "Reader!Pass123"}},
}

c = httpx.Client(base_url=BASE, timeout=None)
tok = c.post("/api/auth/jwt/login", data={"username": "admin@example.com", "password": "Password123!"}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}


def seed():
    if not any(p.get("provider_type") == "anthropic" and p.get("name") == "Loop Haiku"
               for p in c.get("/api/llm/providers", headers=H).json()):
        c.post("/api/llm/providers", headers=H, json={
            "name": "Loop Haiku", "provider_type": "anthropic",
            "credentials": {"api_key": os.environ["ANTHROPIC_API_KEY"]},
            "models": [{"name": "Claude Haiku 5.5", "model_id": "claude-haiku-5-5", "is_enabled": True,
                        "is_default": True, "is_small_default": True}]}).raise_for_status()
    ids = {}
    for key, body in AGENTS.items():
        existing = [d for d in c.get("/api/data_sources", headers=H).json() if d["name"] == body["name"]]
        ids[key] = existing[0]["id"] if existing else c.post(
            "/api/data_sources", headers=H, json={**body, "is_public": True}).json()["id"]
    time.sleep(20)  # first index runs in the background
    for key, ds in ids.items():
        r = c.post(f"/api/data_sources/{ds}/bulk_update_tables", headers=H, json={"action": "activate"})
        print(key, r.json())
    STATE.write_text(json.dumps(ids))


def ask(key, prompt):
    ids = json.loads(STATE.read_text())
    rid = c.post("/api/reports", headers=H, json={"title": f"routines loop {key}", "data_sources": [ids[key]]}).json()["id"]
    with c.stream("POST", f"/api/reports/{rid}/completions", headers={**H, "Accept": "text/event-stream"},
                  json={"prompt": {"content": prompt}, "stream": True}) as resp:
        for _ in resp.iter_lines():
            pass
    items = c.get(f"/api/reports/{rid}/completions", headers=H).json()
    items = items if isinstance(items, list) else items.get("completions") or items.get("items") or []
    for comp in items:
        for b in comp.get("completion_blocks") or []:
            te = b.get("tool_execution") or {}
            if te.get("tool_name"):
                print(f"[tool] {te['tool_name']} {json.dumps(te.get('arguments_json'))[:300]}")
            if b.get("content"):
                print(f"[text] {b['content']}")


if __name__ == "__main__":
    if sys.argv[1] == "seed":
        seed()
    else:
        ask(sys.argv[2], sys.argv[3])
