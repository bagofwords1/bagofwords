#!/usr/bin/env python3
"""Set up the Postgres per-user-copies loop on a running stack, through the API.

Creates a `user_required` Postgres agent over the RLS fixture
(tools/agent/user_copies/pg_rls_fixture.sql), three members whose stored
database logins see different slices, turns custom tables on, and creates one
custom table over the protected fact table. Prints the ids as JSON.

    cd backend && uv run python ../tools/agent/user_copies/pg_setup.py

Env: BOW_BASE_URL (http://localhost:8000), BOW_ADMIN_EMAIL / BOW_ADMIN_PASSWORD,
UC_PG_HOST/PORT/DB (127.0.0.1 / 55432 / rlsdemo), UC_PG_ADMIN_USER / UC_PG_ADMIN_PASSWORD
(the connection's system login; a superuser, so it bypasses RLS), UC_PG_USER_PASSWORD
(the fixture logins' password), UC_DB_PATH (sqlite app db, for invite tokens),
UC_TABLE_SQL (default: SELECT * FROM sales).

Needs an enterprise license (per-user sign-in on a warehouse connection).
"""
import json
import os
import pathlib
import sqlite3
import sys
import uuid

import httpx

BASE = os.environ.get("BOW_BASE_URL", "http://localhost:8000")
ADMIN = os.environ.get("BOW_ADMIN_EMAIL", "admin@example.com")
PASSWORD = os.environ.get("BOW_ADMIN_PASSWORD", "Password123!")
PG = {
    "host": os.environ.get("UC_PG_HOST", "127.0.0.1"),
    "port": int(os.environ.get("UC_PG_PORT", "55432")),
    "database": os.environ.get("UC_PG_DB", "rlsdemo"),
}
PG_ADMIN = os.environ.get("UC_PG_ADMIN_USER", "postgres")
PG_ADMIN_PW = os.environ["UC_PG_ADMIN_PASSWORD"]
PG_USER_PW = os.environ["UC_PG_USER_PASSWORD"]
DB_PATH = os.environ.get(
    "UC_DB_PATH", str(pathlib.Path(__file__).resolve().parents[3] / "backend" / "db" / "agent.db"))
TABLE_SQL = os.environ.get("UC_TABLE_SQL", "SELECT * FROM sales")
# BOW member -> the database login they store.
MEMBERS = {"demo1@example.com": "demo1", "demo2@example.com": "demo2", "demo3@example.com": "demo3"}

c = httpx.Client(base_url=BASE, timeout=300)


def login(email: str) -> str:
    r = c.post("/api/auth/jwt/login", data={"username": email, "password": PASSWORD})
    r.raise_for_status()
    return r.json()["access_token"]


def headers(token: str, org: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": org}


def ok(r: httpx.Response, what: str) -> httpx.Response:
    if r.status_code >= 300:
        sys.exit(f"{what} failed: {r.status_code} {r.text[:600]}")
    return r


admin_tok = login(ADMIN)
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {admin_tok}"}).json()[0]["id"]
ah = headers(admin_tok, org)

ok(c.put("/api/organization/settings",
         json={"config": {"enable_custom_queries": {"value": True}}}, headers=ah), "enable custom tables")

# Members: invite, then register with the invite token read from the app db.
for email in MEMBERS:
    if c.post("/api/auth/jwt/login", data={"username": email, "password": PASSWORD}).status_code == 200:
        continue
    ok(c.post(f"/api/organizations/{org}/members",
              json={"organization_id": org, "email": email, "role": "member"}, headers=ah), f"invite {email}")
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute("SELECT invite_token FROM memberships WHERE email = ? AND invite_token IS NOT NULL "
                         "ORDER BY created_at DESC LIMIT 1", (email,)).fetchone()
    ok(c.post("/api/auth/register", json={
        "name": email.split("@")[0], "email": email, "password": PASSWORD,
        "invite_token": row[0] if row else None}), f"register {email}")

ds = ok(c.post("/api/data_sources", json={
    "name": f"Sales RLS {uuid.uuid4().hex[:4]}",
    "type": "postgresql",
    "config": PG,
    "credentials": {"user": PG_ADMIN, "password": PG_ADMIN_PW},
    "auth_policy": "user_required",
    "allowed_user_auth_modes": ["userpass"],
    "is_public": True,
}, headers=ah), "create data source").json()
ds_id = ds["id"]
conns = ok(c.get(f"/api/data_sources/{ds_id}/connections", headers=ah), "list connections").json()
conn = conns[0]
conn_id = conn["id"]

for email, db_login in MEMBERS.items():
    mh = headers(login(email), org)
    ok(c.post(f"/api/data_sources/{ds_id}/my-credentials", json={
        "auth_mode": "userpass", "credentials": {"user": db_login, "password": PG_USER_PW},
    }, headers=mh), f"store credentials for {email}")

cq = ok(c.post(f"/api/connections/{conn_id}/custom-queries", json={
    "name": "sales_cached",
    "definition_sql": TABLE_SQL,
    "description": "Sales rows the signed-in person may see.",
    "refresh_schedule_mode": "interval",
    "refresh_interval_minutes": 60,
    "activate_for_datasource_id": ds_id,
}, headers=ah), "create custom table").json()

print(json.dumps({
    "organization_id": org, "data_source_id": ds_id, "data_source_name": ds["name"],
    "connection_id": conn_id, "connection_name": conn.get("name"),
    "custom_queries_supported": conn.get("custom_queries_supported"),
    "custom_table": {k: cq.get(k) for k in ("id", "name", "per_user", "user_copies", "last_refresh_status",
                                             "no_rows", "artifact_bytes", "next_run_at")},
    "columns": [col.get("name") for col in cq.get("columns") or []],
    "members": MEMBERS,
}, indent=2))
