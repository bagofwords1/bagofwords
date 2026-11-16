"""Seed the live 'connection queries' scenario through the real API.

admin   (full admin)          — admin@example.com
dba     (manage_connection)   — dba@example.com, on both warehouse connections
analyst (plain member)        — analyst@example.com

LLM: Anthropic Claude Haiku 5.5 when ANTHROPIC_API_KEY is set; otherwise
register tools/agent/stop_query_stub_llm.py with setup_stub_llm.py.
Agents: "Warehouse (Postgres)" limit 2, "Sales (SQL Server)" limit 1.

    cd backend && uv run python ../tools/agent/connection_queries/seed.py <out/live.json>

Expects the stack up (backend on :8000, app DB `bow_live` on local Postgres for
the invite-token lookup), seed_org.py already run, a local Postgres database
`warehouse` with public.orders, and SQL Server with sales.dbo.orders.
"""
import json
import os
import subprocess
import sys
import time

import httpx

BASE = "http://localhost:8000"
PW = "Password123!"
c = httpx.Client(base_url=BASE, timeout=120)


def login(email):
    r = c.post("/api/auth/jwt/login", data={"username": email, "password": PW})
    r.raise_for_status()
    return r.json()["access_token"]


admin = login("admin@example.com")
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {admin}"}).json()[0]["id"]
H = {"Authorization": f"Bearer {admin}", "X-Organization-Id": org}


def psql(sql):
    return subprocess.check_output(
        ["psql", "-h", "127.0.0.1", "-U", "postgres", "-d", "bow_live", "-tAc", sql],
        env={**os.environ, "PGPASSWORD": "postgres"}, text=True).strip()


def ensure_member(email, name):
    try:
        return login(email)
    except httpx.HTTPStatusError:
        pass
    r = c.post(f"/api/organizations/{org}/members", headers=H,
               json={"organization_id": org, "email": email, "role": "member"})
    assert r.status_code == 200, r.text
    token = psql(f"select invite_token from memberships where email='{email}' and invite_token is not null limit 1")
    r = c.post("/api/auth/register", json={"name": name, "email": email, "password": PW, "invite_token": token})
    assert r.status_code in (200, 201), r.text
    return login(email)


dba = ensure_member("dba@example.com", "Dana DBA")
analyst = ensure_member("analyst@example.com", "Avi Analyst")

# --- LLM: Haiku 5.5 (optional) ------------------------------------------------
key = os.environ.get("ANTHROPIC_API_KEY")
if key:
    providers = c.get("/api/llm/providers", headers=H).json()
    prov = next((p for p in providers if p.get("provider_type") == "anthropic"), None)
    if not prov:
        r = c.post("/api/llm/providers", headers=H, json={
            "name": "Anthropic", "provider_type": "anthropic", "credentials": {"api_key": key}})
        assert r.status_code in (200, 201), r.text
        prov = r.json()
    models = c.get("/api/llm/models", headers=H).json()
    m = next((m for m in models if m.get("model_id") == "claude-haiku-5-5"), None)
    if not m:
        r = c.post("/api/llm/models", headers=H, json={
            "provider_id": prov["id"], "name": "Claude Haiku 5.5", "model_id": "claude-haiku-5-5",
            "is_default": True, "is_small_default": True})
        assert r.status_code in (200, 201), r.text
        m = r.json()
    for small in (False, True):
        c.post(f"/api/llm/models/{m['id']}/set_default", params={"small": str(small).lower()}, headers=H).raise_for_status()
    for other in c.get("/api/llm/models", headers=H).json():
        if other["id"] != m["id"] and other.get("is_enabled"):
            c.put(f"/api/llm/models/{other['id']}", headers=H, json={"is_enabled": False})
    print("default model:", [x["model_id"] for x in c.get("/api/llm/models", headers=H).json() if x.get("is_default")])


# --- Agents over the two warehouses ------------------------------------------
def ensure_agent(name, type_, config, credentials, table):
    existing = [d for d in c.get("/api/data_sources", headers=H).json() if d["name"] == name]
    if existing:
        ds = existing[0]
    else:
        r = c.post("/api/data_sources", headers=H, json={
            "name": name, "type": type_, "config": config, "credentials": credentials,
            "auth_policy": "system_only", "is_public": True,
            "generate_summary": False, "generate_conversation_starters": False, "generate_ai_rules": False})
        assert r.status_code == 200, r.text
        ds = r.json()
    ds_id = ds["id"]
    for _ in range(60):
        full = c.get(f"/api/data_sources/{ds_id}/full_schema", headers=H).json()
        if full:
            break
        time.sleep(2)
    names = [t["name"] for t in full]
    want = [n for n in names if n.endswith(table)]
    r = c.put(f"/api/data_sources/{ds_id}/update_tables_status", headers=H, json={"activate": want})
    assert r.status_code == 200, r.text
    detail = c.get(f"/api/data_sources/{ds_id}", headers=H).json()
    conn_id = detail["connections"][0]["id"]
    return ds_id, conn_id, want


pg_ds, pg_conn, pg_tables = ensure_agent(
    "Warehouse (Postgres)", "postgresql",
    {"host": "127.0.0.1", "port": 5432, "database": "warehouse", "sslmode": "disable", "max_concurrent_queries": 2},
    {"user": "postgres", "password": "postgres"}, "orders")
ms_ds, ms_conn, ms_tables = ensure_agent(
    "Sales (SQL Server)", "MSSQL",
    {"host": "127.0.0.1", "port": 1433, "database": "sales", "odbc_driver": 18, "encrypt": False,
     "max_concurrent_queries": 1},
    {"user": "sa", "password": os.environ["MSSQL_SA_PASSWORD"]}, "orders")

# Names and the per-connection limits go through the real update endpoint
# (the connection's "Query limits" setting).
for conn_id, name, limit in ((pg_conn, "warehouse-pg", 2), (ms_conn, "sales-mssql", 1)):
    r = c.put(f"/api/connections/{conn_id}", headers=H, json={"name": name, "max_concurrent_queries": limit})
    assert r.status_code == 200, r.text

for conn_id in (pg_conn, ms_conn):
    r = c.post(f"/api/organizations/{org}/resource-grants", headers=H, json={
        "resource_type": "connection", "resource_id": conn_id, "principal_type": "user",
        "principal_id": psql("select id from users where email='dba@example.com'"),
        "permissions": ["manage_connection"]})
    if r.status_code != 200:
        print("grant failed", r.status_code, r.text[:200])

out = {"org": org, "pg": {"ds": pg_ds, "conn": pg_conn, "tables": pg_tables},
       "mssql": {"ds": ms_ds, "conn": ms_conn, "tables": ms_tables}}
print(json.dumps(out, indent=2))
with open(sys.argv[1], "w") as f:
    json.dump(out, f, indent=2)
