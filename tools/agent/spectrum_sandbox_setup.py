#!/usr/bin/env python3
"""Create the DX NetOps Spectrum (mock) agent in a running BOW org via the API,
wait for schema indexing, and activate every discovered table.

Prereqs: tools/agent/boot_stack.sh, tools/agent/seed_org.py, the mock
(`tools/spectrum/docker-compose.yaml` or
`uv run --project backend uvicorn mock_oneclick:app --port 8880`), and an
enterprise license active (the connector is enterprise-gated).

    cd backend && uv run python ../tools/agent/spectrum_sandbox_setup.py

Env: BOW_BASE_URL (default http://localhost:8000), BOW_ADMIN_EMAIL /
BOW_ADMIN_PASSWORD (seed_org defaults), SPECTRUM_MOCK_URL (default
http://127.0.0.1:8880), SPECTRUM_MOCK_USER / SPECTRUM_MOCK_PASSWORD (mock
read-only user), SPECTRUM_AUTH=userpass|token (+ SPECTRUM_MOCK_TOKEN).
Prints a JSON summary with the data source id, connection id and the table
list. Idempotent: reuses an existing data source with the same name.
"""
import json
import os
import sys
import time

import httpx

BASE = os.environ.get("BOW_BASE_URL", "http://localhost:8000")
EMAIL = os.environ.get("BOW_ADMIN_EMAIL", "admin@example.com")
PASSWORD = os.environ.get("BOW_ADMIN_PASSWORD", "Password123!")
NAME = os.environ.get("SPECTRUM_DS_NAME", "Spectrum (prod)")
MOCK_URL = os.environ.get("SPECTRUM_MOCK_URL", "http://127.0.0.1:8880")
MOCK_USER = os.environ.get("SPECTRUM_MOCK_USER", "bow_api")
MOCK_PASSWORD = os.environ.get("SPECTRUM_MOCK_PASSWORD", "Spectrum!2024")
AUTH = os.environ.get("SPECTRUM_AUTH", "userpass")

c = httpx.Client(base_url=BASE, timeout=180, trust_env=False)
tok = c.post("/api/auth/jwt/login", data={"username": EMAIL, "password": PASSWORD}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}

# 0. The connector must be in the catalog and unlocked by the license.
catalog = c.get("/api/available_data_sources", headers=H).json()
entry = next((d for d in catalog if d.get("type") == "spectrum"), None)
if not entry:
    sys.exit("spectrum is not in /api/available_data_sources")
print("catalog entry:", {k: entry.get(k) for k in ("type", "title", "category", "requires_license", "version")})
print("auth variants:", [v.get("key") or v.get("type") or v for v in (entry.get("credentials_auth") or {}).get("variants", [])] or entry.get("auth_types"))

# 1. Data source (agent) with an inline connection.
existing = next((d for d in c.get("/api/data_sources", headers=H).json() if d.get("name") == NAME), None)
if existing:
    ds = existing
    print("reusing data source", ds["id"])
else:
    creds = {"token": os.environ.get("SPECTRUM_MOCK_TOKEN", "")} if AUTH == "token" else \
        {"username": MOCK_USER, "password": MOCK_PASSWORD}
    r = c.post("/api/data_sources", json={
        "name": NAME,
        "type": "spectrum",
        # auth_type travels inside config (data_source_registry resolves the variant from it)
        "config": {"url": MOCK_URL, "landscapes": "", "verify_ssl": False, "request_timeout_s": 600,
                   "throttlesize": 500, "auth_type": AUTH},
        "credentials": creds,
        "auth_policy": "system_only",
        # Public so the home composer's Auto scope (every agent the user can
        # see) picks it up; a private agent must be pinned explicitly.
        "is_public": True,
    }, headers=H)
    if r.status_code not in (200, 201):
        sys.exit(f"create data source failed: {r.status_code} {r.text}")
    ds = r.json()
    print("created data source", ds["id"])

# 2. Test connection through the product path.
t = c.get(f"/api/data_sources/{ds['id']}/test_connection", headers=H)
print("test_connection:", t.status_code, t.text[:240])

# 3. Wait for indexing on the linked connection(s).
conns = ds.get("connections") or []
conn_ids = [x.get("id") for x in conns if x.get("id")]
if not conn_ids and ds.get("connection_id"):
    conn_ids = [ds["connection_id"]]
for cid in conn_ids:
    for _ in range(60):
        p = c.get(f"/api/connections/{cid}/indexing", headers=H)
        body = p.json() if p.status_code == 200 else {}
        status = body.get("status") or body.get("state")
        print("indexing:", cid[:8], status, body.get("done"), "/", body.get("total"))
        if p.status_code == 200 and (status in ("completed", "complete", "success", "done", "idle") or (status is None and not body)):
            break
        if status in ("failed", "error", "cancelled"):
            sys.exit(f"indexing failed: {body}")
        time.sleep(3)

# 4. Activate every table (the Tables selector, via API).
tables = []
for _ in range(20):
    r = c.get(f"/api/data_sources/{ds['id']}/full_schema", headers=H)
    tables = r.json() if r.status_code == 200 else []
    if isinstance(tables, dict):
        tables = tables.get("items") or tables.get("tables") or []
    if tables:
        break
    time.sleep(3)
if not tables:
    r = c.get(f"/api/data_sources/{ds['id']}/refresh_schema", headers=H)
    print("refresh_schema:", r.status_code, (r.text[:120] if r.status_code != 200 else f"{len(r.json())} tables"))
    r = c.get(f"/api/data_sources/{ds['id']}/full_schema", headers=H)
    tables = r.json() if r.status_code == 200 else []
    if isinstance(tables, dict):
        tables = tables.get("items") or tables.get("tables") or []
if not tables:
    sys.exit("no tables discovered")
for tb in tables:
    tb["is_active"] = True
    tb.setdefault("datasource_id", ds["id"])
r = c.put(f"/api/data_sources/{ds['id']}/update_schema", json=tables, headers=H)
print("update_schema:", r.status_code, r.text[:120] if r.status_code != 200 else "ok")
active = c.get(f"/api/data_sources/{ds['id']}/schema", headers=H).json()
print(json.dumps({"data_source_id": ds["id"], "connection_ids": conn_ids,
                  "tables_discovered": len(tables), "tables_active": len(active),
                  "table_names": [t["name"] for t in tables]}, indent=1))
