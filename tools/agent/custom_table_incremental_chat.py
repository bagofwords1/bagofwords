#!/usr/bin/env python3
"""Ask the agent a question it can only get right from a freshly incremented
custom table — the end of the incremental feedback loop.

For each engine: change rows at the source, refresh the custom table through
the API (an incremental run), then ask the agent (a real LLM) how many rows
carry the new marker. The count exists nowhere but in the rows the increment
just brought in, so a right answer means the whole path worked: source →
incremental refresh → cached table → the agent's SQL → the reply. Then change
the source again and ask again: the answer must move.

Prereqs: the stack, a seeded org, an LLM (tools/agent/setup_haiku_llm.py),
and tools/agent/custom_table_incremental_setup.py.

    cd backend && INCR_LOOP_PG_URL=... INCR_LOOP_MSSQL_URL=... \\
        uv run python ../tools/agent/custom_table_incremental_chat.py

Exit code 0 only if every answer matched the source.
"""
import json
import os
import re
import sys
import time
import uuid

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

BASE = os.environ.get("BOW_BASE_URL", "http://localhost:8000")
EMAIL = os.environ.get("BOW_ADMIN_EMAIL", "admin@example.com")
PASSWORD = os.environ.get("BOW_ADMIN_PASSWORD", "Password123!")
TABLE = os.environ.get("INCR_LOOP_TABLE", "orders_cache")

c = httpx.Client(base_url=BASE, timeout=900)
tok = c.post("/api/auth/jwt/login", data={"username": EMAIL, "password": PASSWORD}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}


def sources():
    pg, mssql = os.environ.get("INCR_LOOP_PG_URL"), os.environ.get("INCR_LOOP_MSSQL_URL")
    if pg:
        yield {"label": "postgres", "ds_name": "Shop (Postgres)", "url": pg, "orders": "shop.orders",
               "now": "(now() AT TIME ZONE 'UTC')"}
    if mssql:
        url = make_url(mssql).set(database="shop").render_as_string(hide_password=False)
        yield {"label": "mssql", "ds_name": "Shop (SQL Server)", "url": url, "orders": "dbo.orders",
               "now": "SYSUTCDATETIME()"}


def ask(ds_id: str, prompt: str) -> dict:
    report = c.post("/api/reports", headers=H, json={
        "title": prompt[:60], "widget": None, "files": [], "data_sources": [ds_id]}).json()
    started = time.monotonic()
    r = c.post(f"/api/reports/{report['id']}/completions", headers=H, params={"background": False},
               json={"prompt": {"content": prompt, "widget_id": None, "step_id": None, "mentions": [{}]}})
    listing = c.get(f"/api/reports/{report['id']}/completions", headers=H).json()
    items = listing if isinstance(listing, list) else listing.get("completions") or listing.get("items") or []
    answer, queries = "", []
    for comp in items:
        if comp.get("role") == "user":
            continue
        body = comp.get("completion")
        if isinstance(body, dict) and body.get("content"):
            answer = body["content"]
        for block in comp.get("completion_blocks") or []:
            if block.get("content") and len(block["content"]) > 3:
                answer = block["content"]
            dump = json.dumps(block.get("tool_execution") or {}, default=str)
            if TABLE in dump:
                queries.append((block.get("tool_execution") or {}).get("tool_name"))
    return {"http": r.status_code, "seconds": round(time.monotonic() - started, 1), "answer": answer,
            "tools_that_read_the_cache": queries, "report_id": report["id"]}


def numbers(text_: str) -> set:
    return {int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", text_ or "")}


results, failed = [], False
for src in sources():
    ds = next((d for d in c.get("/api/data_sources", headers=H).json() if d.get("name") == src["ds_name"]), None)
    if not ds:
        sys.exit(f"{src['ds_name']} is not set up; run custom_table_incremental_setup.py first")
    conn_id = (ds.get("connections") or [{}])[0].get("id") or ds.get("connection_id")
    table = next(t for t in c.get(f"/api/connections/{conn_id}/custom-queries", headers=H).json()
                 if t["name"] == TABLE)
    engine = create_engine(src["url"], isolation_level="AUTOCOMMIT")
    marker = f"loop-{uuid.uuid4().hex[:6]}"

    for round_, modulus in enumerate((9001, 4001), start=1):
        with engine.connect() as conn:
            conn.execute(text(
                f"UPDATE {src['orders']} SET status = :m, updated_at = {src['now']} WHERE id % :k = 5"),
                {"m": marker, "k": modulus})
            truth = conn.execute(text(f"SELECT COUNT(*) FROM {src['orders']} WHERE status = :m"),
                                 {"m": marker}).scalar()
        refreshed = c.post(f"/api/connections/{conn_id}/custom-queries/{table['id']}/refresh", headers=H)
        if refreshed.status_code != 200:
            sys.exit(f"refresh failed: {refreshed.status_code} {refreshed.text[:300]}")
        run = c.get(f"/api/connections/{conn_id}/custom-queries/{table['id']}/runs", headers=H,
                    params={"limit": 1}).json()["runs"][0]
        turn = ask(ds["id"], f"Using the {TABLE} table, how many orders have the status '{marker}'? "
                             f"Reply with the number.")
        ok = turn["http"] == 200 and truth in numbers(turn["answer"]) and bool(turn["tools_that_read_the_cache"])
        failed = failed or not ok
        results.append({
            "engine": src["label"], "round": round_, "rows_with_marker_at_source": truth,
            "refresh": {"mode": run["mode"], "status": run["status"], "rows_read": run["rows_read"],
                        "rows_updated": run["rows_updated"], "elapsed_ms": run["elapsed_ms"]},
            "agent": turn, "answer_matches_source": ok,
        })
        print(f"{'PASS' if ok else 'FAIL'}  {src['label']} round {round_}: source={truth} "
              f"refresh={run['mode']} read {run['rows_read']} rows in {run['elapsed_ms']} ms; "
              f"agent ({turn['seconds']}s): {turn['answer'][:160]!r}")
    engine.dispose()

if not results:
    sys.exit("Set INCR_LOOP_PG_URL and/or INCR_LOOP_MSSQL_URL.")
out = os.environ.get("INCR_LOOP_CHAT_REPORT")
if out:
    with open(out, "w") as f:
        json.dump(results, f, indent=2, default=str)
sys.exit(1 if failed else 0)
