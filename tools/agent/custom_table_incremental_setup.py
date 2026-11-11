#!/usr/bin/env python3
"""Connect the seeded `shop` databases to a running BOW org and create
incremental custom tables on them — through the real API.

Prereqs: tools/agent/boot_stack.sh, tools/agent/seed_org.py, the databases
seeded by tools/agent/custom_table_incremental_seed.py, and (for the chat
step of the loop) an LLM configured, e.g. tools/agent/setup_haiku_llm.py.

    cd backend && INCR_LOOP_PG_URL=... INCR_LOOP_MSSQL_URL=... \\
        uv run python ../tools/agent/custom_table_incremental_setup.py

Env: BOW_BASE_URL (default http://localhost:8000), BOW_ADMIN_EMAIL /
BOW_ADMIN_PASSWORD (seed_org defaults). Prints a JSON summary: per engine the
data source id, connection id and the custom table as the API returned it.
Idempotent: reuses a data source or custom table with the same name.
"""
import json
import os
import sys
import time

import httpx
from sqlalchemy.engine import make_url

BASE = os.environ.get("BOW_BASE_URL", "http://localhost:8000")
EMAIL = os.environ.get("BOW_ADMIN_EMAIL", "admin@example.com")
PASSWORD = os.environ.get("BOW_ADMIN_PASSWORD", "Password123!")
TABLE_NAME = os.environ.get("INCR_LOOP_TABLE", "orders_cache")

c = httpx.Client(base_url=BASE, timeout=600)
tok = c.post("/api/auth/jwt/login", data={"username": EMAIL, "password": PASSWORD}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}

r = c.put("/api/organization/settings", json={"config": {"enable_custom_queries": {"value": True}}}, headers=H)
if r.status_code != 200:
    sys.exit(f"could not enable custom tables: {r.status_code} {r.text[:300]}")


def engines():
    pg, mssql = os.environ.get("INCR_LOOP_PG_URL"), os.environ.get("INCR_LOOP_MSSQL_URL")
    if pg:
        u = make_url(pg)
        yield {
            "name": "Shop (Postgres)", "type": "postgresql", "orders": "shop.orders",
            "config": {"host": u.host, "port": u.port or 5432, "database": u.database, "schema": "shop"},
            "credentials": {"user": u.username, "password": u.password},
        }
    if mssql:
        u = make_url(mssql)
        yield {
            "name": "Shop (SQL Server)", "type": "MSSQL", "orders": "dbo.orders",
            # The throwaway container has a self-signed certificate.
            "config": {"host": u.host, "port": u.port or 1433, "database": "shop", "encrypt": False},
            "credentials": {"user": u.username, "password": u.password},
        }


def data_source(engine: dict) -> dict:
    existing = next((d for d in c.get("/api/data_sources", headers=H).json() if d.get("name") == engine["name"]), None)
    if existing:
        return existing
    r = c.post("/api/data_sources", json={
        "name": engine["name"], "type": engine["type"], "config": engine["config"],
        "credentials": engine["credentials"], "auth_policy": "system_only",
    }, headers=H)
    if r.status_code not in (200, 201):
        sys.exit(f"create data source failed: {r.status_code} {r.text[:500]}")
    return r.json()


def connection_id(ds: dict) -> str:
    conns = ds.get("connections") or []
    ids = [x.get("id") for x in conns if x.get("id")] or ([ds["connection_id"]] if ds.get("connection_id") else [])
    if not ids:
        full = c.get(f"/api/data_sources/{ds['id']}", headers=H).json()
        ids = [x.get("id") for x in (full.get("connections") or []) if x.get("id")]
    if not ids:
        sys.exit(f"data source {ds['id']} has no connection")
    return ids[0]


def wait_indexed(cid: str) -> None:
    for _ in range(60):
        p = c.get(f"/api/connections/{cid}/indexing", headers=H)
        body = p.json() if p.status_code == 200 else {}
        status = (body or {}).get("status") or (body or {}).get("state")
        if status in ("completed", "complete", "success", "done", "idle", None):
            return
        if status in ("failed", "error", "cancelled"):
            sys.exit(f"indexing failed: {body}")
        time.sleep(2)


def custom_table(cid: str, ds_id: str, engine: dict) -> dict:
    base = f"/api/connections/{cid}/custom-queries"
    listing = c.get(base, headers=H)
    if listing.status_code != 200:
        sys.exit(f"list custom tables failed: {listing.status_code} {listing.text[:300]}")
    existing = next((t for t in listing.json() if t["name"] == TABLE_NAME), None)
    if existing:
        return existing
    r = c.post(base, headers=H, json={
        "name": TABLE_NAME,
        "description": "Every order, kept current incrementally by updated_at.",
        "definition_sql": f"SELECT id, customer_id, customer_name, amount, currency, status, updated_at "
                          f"FROM {engine['orders']}",
        "refresh_schedule_mode": "interval", "refresh_interval_minutes": 60,
        "refresh_mode": "merge",
        "refresh_config": {"cursor_column": "updated_at", "unique_key": ["id"], "lookback_seconds": 86400},
        "activate_for_datasource_id": ds_id,
    })
    if r.status_code != 200:
        sys.exit(f"create custom table failed: {r.status_code} {r.text[:500]}")
    return r.json()


summary = {}
for engine in engines():
    ds = data_source(engine)
    cid = connection_id(ds)
    wait_indexed(cid)
    started = time.time()
    table = custom_table(cid, ds["id"], engine)
    summary[engine["type"]] = {
        "data_source_id": ds["id"], "connection_id": cid,
        "custom_table": {k: table.get(k) for k in (
            "id", "name", "no_rows", "refresh_mode", "checkpoint", "last_refresh_status",
            "last_refresh_ms", "artifact_bytes", "load_progress")},
        "create_seconds": round(time.time() - started, 1),
    }
if not summary:
    sys.exit("Set INCR_LOOP_PG_URL and/or INCR_LOOP_MSSQL_URL.")
print(json.dumps(summary, indent=2, default=str))
