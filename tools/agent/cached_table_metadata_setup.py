#!/usr/bin/env python3
"""Connect the synthetic `retail` Postgres database to a running BOW org as an
agent, and print what indexing stored for its columns — through the real API.

Used by docs/feedback-loops/cached-table-column-metadata.md. Prereqs:
tools/agent/boot_stack.sh, tools/agent/seed_org.py, and a Postgres database
seeded with tools/agent/cached_table_metadata_seed.sql.

    cd backend && RETAIL_PG_URL=postgresql://user:pass@127.0.0.1:5432/retail \\
        uv run python ../tools/agent/cached_table_metadata_setup.py

Env: BOW_BASE_URL (default http://localhost:8000), BOW_ADMIN_EMAIL /
BOW_ADMIN_PASSWORD (seed_org defaults). Idempotent: reuses the agent by name.
Prints a JSON summary: the agent and connection ids and, per indexed table,
each column's comment as the catalog holds it.
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
NAME = os.environ.get("RETAIL_AGENT_NAME", "Retail")
URL = os.environ.get("RETAIL_PG_URL")
if not URL:
    sys.exit("Set RETAIL_PG_URL.")

c = httpx.Client(base_url=BASE, timeout=600)
tok = c.post("/api/auth/jwt/login", data={"username": EMAIL, "password": PASSWORD}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}

ds = next((d for d in c.get("/api/data_sources", headers=H).json() if d.get("name") == NAME), None)
if ds is None:
    u = make_url(URL)
    r = c.post("/api/data_sources", headers=H, json={
        "name": NAME, "type": "postgresql",
        "config": {"host": u.host, "port": u.port or 5432, "database": u.database, "schema": "public"},
        "credentials": {"user": u.username, "password": u.password},
        "auth_policy": "system_only",
    })
    if r.status_code not in (200, 201):
        sys.exit(f"create agent failed: {r.status_code} {r.text[:500]}")
    ds = r.json()

full = c.get(f"/api/data_sources/{ds['id']}", headers=H).json()
connections = [x for x in (full.get("connections") or []) if x.get("id")]
if not connections:
    sys.exit(f"agent {ds['id']} has no connection")
cid = connections[0]["id"]

for _ in range(60):
    p = c.get(f"/api/connections/{cid}/indexing", headers=H)
    status = (p.json() or {}).get("status") if p.status_code == 200 else None
    if status in ("completed", "complete", "success", "done", "idle", None):
        break
    if status in ("failed", "error", "cancelled"):
        sys.exit(f"indexing failed: {p.text[:300]}")
    time.sleep(2)

tables = c.get(f"/api/data_sources/{ds['id']}/full_schema", headers=H, params={"page": 1, "page_size": 100})
if tables.status_code != 200:
    sys.exit(f"list tables failed: {tables.status_code} {tables.text[:300]}")
body = tables.json()
rows = body.get("tables") if isinstance(body, dict) else body
print(json.dumps({
    "data_source_id": ds["id"], "connection_id": cid,
    "catalog": {
        t["name"]: {col["name"]: col.get("description") for col in t.get("columns") or []}
        for t in rows or [] if t.get("name")
    },
}, indent=2, default=str))
