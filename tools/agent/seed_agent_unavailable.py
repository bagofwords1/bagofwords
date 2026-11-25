#!/usr/bin/env python3
"""Seed the agents for the "agent the run cannot load" loop
(docs/feedback-loops/2026-11-21-agent-unavailable-followups.md), through the
real API as the admin.

    cd backend && uv run python ../tools/agent/seed_agent_unavailable.py \
        --pg-port 5544 --pg-password … --member member@example.com

Creates (idempotent by name):
  * Sales      system_only SQLite (Chinook). The loop marks its connection
               unreachable with --sales-down / --sales-up.
  * Inventory  system_only SQLite, always reachable.
  * Mailbox    Postgres, each person signs in with their own login, marked as
               a connector (the person's own account).
  * Finance    Postgres, each person signs in, placed in the folder
               "Finance team" that the member may view.

Postgres: a local server with database `workspace`, tables `messages` and
`budget`, and the service login given by --pg-user/--pg-password.
"""
import argparse
import pathlib
import sqlite3

import httpx

p = argparse.ArgumentParser()
p.add_argument("--base-url", default="http://localhost:8000")
p.add_argument("--email", default="admin@example.com")
p.add_argument("--password", default="Password123!")
p.add_argument("--member", default="member@example.com")
p.add_argument("--pg-host", default="127.0.0.1")
p.add_argument("--pg-port", type=int, default=5544)
p.add_argument("--pg-db", default="workspace")
p.add_argument("--pg-user", default="bow_service")
p.add_argument("--pg-password", default="")
p.add_argument("--db-path", default="db/agent.db", help="app SQLite file, for --sales-down/--sales-up only")
p.add_argument("--sales-down", action="store_true", help="flag the Sales connection unreachable and exit")
p.add_argument("--sales-up", action="store_true", help="flag the Sales connection reachable and exit")
p.add_argument("--forget-signins", action="store_true", help="drop the member's saved sign-ins and exit (re-run the loop)")
a = p.parse_args()

if a.forget_signins:
    con = sqlite3.connect(a.db_path)
    uid = con.execute("SELECT id FROM users WHERE email = ?", (a.member,)).fetchone()[0]
    n = sum(con.execute(f"DELETE FROM {t} WHERE user_id = ?", (uid,)).rowcount for t in (
        "user_connection_credentials", "user_data_source_credentials",
    ) if con.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (t,)).fetchone())
    con.commit()
    print(f"forgot {n} saved sign-in(s) of {a.member}")
    raise SystemExit(0)

if a.sales_down or a.sales_up:
    # Connection.is_active is the cached health flag a failed probe clears
    # after three failures in a row; set it directly instead of staging an
    # outage of a real server.
    con = sqlite3.connect(a.db_path)
    n = con.execute(
        "UPDATE connections SET is_active = ? WHERE id IN (SELECT dc.connection_id FROM domain_connection dc "
        "JOIN data_sources ds ON ds.id = dc.data_source_id WHERE ds.name = 'Sales')",
        (0 if a.sales_down else 1,),
    ).rowcount
    con.commit()
    print("Sales connection", "unreachable" if a.sales_down else "reachable", f"({n} row)")
    raise SystemExit(0 if n else 1)

c = httpx.Client(base_url=a.base_url, timeout=120, trust_env=False)
tok = c.post("/api/auth/jwt/login", data={"username": a.email, "password": a.password}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}
chinook = str((pathlib.Path(__file__).resolve().parents[2] / "backend/tests/config/chinook.sqlite"))


def ok(r):
    if not r.is_success:
        raise SystemExit(f"{r.request.method} {r.request.url} -> {r.status_code} {r.text[:400]}")
    return r.json()


existing = {d["name"]: d for d in ok(c.get("/api/data_sources", headers=H))}


def source(name, body, **update):
    ds = existing.get(name) or ok(c.post("/api/data_sources", headers=H, json={"name": name, **body}))
    ok(c.get(f"/api/data_sources/{ds['id']}/refresh_schema", headers=H))
    ok(c.put(f"/api/data_sources/{ds['id']}", headers=H, json={"is_public": True, **update}))
    ok(c.post(f"/api/data_sources/{ds['id']}/bulk_update_tables", headers=H, json={"action": "activate", "filter": None}))
    print(f"{name}: {ds['id']}")
    return ds


sqlite_body = {"type": "sqlite", "config": {"database": chinook}, "credentials": {}, "auth_policy": "system_only"}
pg_body = {
    "type": "postgresql",
    "config": {"host": a.pg_host, "port": a.pg_port, "database": a.pg_db, "schema": "public"},
    "credentials": {"user": a.pg_user, "password": a.pg_password},
    "auth_policy": "user_required",
    "allowed_user_auth_modes": ["userpass"],
}
source("Sales", sqlite_body, description="Orders, invoices and revenue by region.")
source("Inventory", sqlite_body, description="Stock levels and catalog.")
source("Mailbox", pg_body, kind="connector", description="Your own mailbox: unread mail, senders, subjects.")
finance = source("Finance", pg_body, description="Department budgets by quarter.")

folders = ok(c.get("/api/folders", headers=H))
folders = folders.get("folders", folders) if isinstance(folders, dict) else folders
folder = next((f for f in folders if f.get("name") == "Finance team"), None) \
    or ok(c.post("/api/folders", headers=H, json={"name": "Finance team"}))
ok(c.put(f"/api/folders/items/agent/{finance['id']}/placement", headers=H, json={"parent_id": folder["id"]}))
members = ok(c.get(f"/api/organizations/{org}/members", headers=H))
member = next(m for m in members if (m.get("email") or (m.get("user") or {}).get("email")) == a.member)
role = ok(c.post(f"/api/organizations/{org}/roles", headers=H, json={
    "name": "Finance readers",
    "resource_grants": [{"resource_type": "folder", "resource_id": folder["id"], "permissions": ["view"]}],
})) if not any(r.get("name") == "Finance readers" for r in ok(c.get(f"/api/organizations/{org}/roles", headers=H))) else None
if role:
    ok(c.post(f"/api/organizations/{org}/role-assignments", headers=H, json={
        "role_id": role["id"], "principal_type": "user", "principal_id": member.get("user_id") or member["user"]["id"]}))
print("folder Finance team:", folder["id"])
