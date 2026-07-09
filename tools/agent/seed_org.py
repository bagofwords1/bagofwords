#!/usr/bin/env python3
"""Seed a running bagofwords backend with an org, users, and (optionally) the
demo data source — through the real HTTP API, so it doubles as a smoke test.

Run it with the backend's venv so httpx is available:

    cd backend && uv run python ../tools/agent/seed_org.py [flags]

Typical agent usage (after tools/agent/boot_stack.sh):

    uv run python ../tools/agent/seed_org.py --demo --invite member@example.com

Flags:
    --base-url    backend URL              (default http://localhost:8000)
    --email       admin email              (default admin@example.com)
    --password    admin password           (default Password123!)
    --name        admin display name       (default Agent Admin)
    --org-name    organization name        (default Agent Org)
    --demo        install the demo data source (first one listed)
    --invite      also invite+register this email as a member (repeatable)
    --db-path     sqlite db file, used only to read invite tokens
                  (default backend/db/agent.db — matches boot_stack.sh)

Prints a JSON summary (tokens included) to stdout. Idempotent-ish: falls back
to login when the user already exists.
"""
import argparse
import json
import pathlib
import sqlite3
import sys

import httpx


def register(client: httpx.Client, name: str, email: str, password: str, invite_token: str | None = None) -> None:
    body = {"name": name, "email": email, "password": password}
    if invite_token:
        body["invite_token"] = invite_token
    r = client.post("/api/auth/register", json=body)
    if r.status_code == 201:
        return
    # 400 REGISTER_USER_ALREADY_EXISTS is fine for idempotent reruns; so is
    # "Sign-up is disabled" when the user already exists (the disabled check
    # fires before the already-exists check) — login below settles it.
    if r.status_code == 400 and ("ALREADY_EXISTS" in r.text or "Sign-up is disabled" in r.text):
        return
    sys.exit(f"register {email} failed: {r.status_code} {r.text}")


def login(client: httpx.Client, email: str, password: str) -> str:
    r = client.post("/api/auth/jwt/login", data={"username": email, "password": password})
    if r.status_code != 200:
        sys.exit(f"login {email} failed: {r.status_code} {r.text}")
    return r.json()["access_token"]


def auth(token: str, org_id: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    if org_id:
        headers["X-Organization-Id"] = org_id
    return headers


def pending_invite_token(db_path: str, email: str) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT invite_token FROM memberships "
            "WHERE email = ? AND user_id IS NULL ORDER BY created_at DESC LIMIT 1",
            (email,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--email", default="admin@example.com")
    p.add_argument("--password", default="Password123!")
    p.add_argument("--name", default="Agent Admin")
    p.add_argument("--org-name", default="Agent Org")
    p.add_argument("--demo", action="store_true")
    p.add_argument(
        "--sqlite-sources",
        type=int,
        default=0,
        metavar="N",
        help="also create N SQLite data sources, each backed by its own seeded "
             ".db file with an orders_<i> table (for multi-source / concurrent "
             "tool-dispatch verification)",
    )
    p.add_argument("--invite", action="append", default=[])
    p.add_argument(
        "--db-path",
        default=str(pathlib.Path(__file__).resolve().parents[2] / "backend" / "db" / "agent.db"),
    )
    args = p.parse_args()

    summary: dict = {"base_url": args.base_url}
    client = httpx.Client(base_url=args.base_url, timeout=30)

    # 1. Admin user (first registered user needs no invite token).
    register(client, args.name, args.email, args.password)
    admin_token = login(client, args.email, args.password)
    summary["admin"] = {"email": args.email, "password": args.password, "token": admin_token}

    # 2. Organization (reuse an existing one on reruns).
    r = client.get("/api/organizations", headers=auth(admin_token))
    orgs = r.json() if r.status_code == 200 else []
    existing = next((o for o in orgs if o.get("name") == args.org_name), None) or (orgs[0] if orgs else None)
    if existing:
        org_id = existing["id"]
    else:
        r = client.post("/api/organizations", json={"name": args.org_name}, headers=auth(admin_token))
        if r.status_code != 200:
            sys.exit(f"create organization failed: {r.status_code} {r.text}")
        org_id = r.json()["id"]
    summary["organization"] = {"id": org_id, "name": args.org_name}

    # 3. Optional demo data source.
    if args.demo:
        r = client.get("/api/data_sources/demos", headers=auth(admin_token, org_id))
        demos = r.json() if r.status_code == 200 else []
        if not demos:
            summary["demo_data_source"] = {"error": f"no demos available ({r.status_code})"}
        else:
            demo_id = demos[0]["id"]
            r = client.post(f"/api/data_sources/demos/{demo_id}", headers=auth(admin_token, org_id))
            summary["demo_data_source"] = (
                r.json() if r.status_code == 200 else {"error": f"{r.status_code} {r.text}"}
            )

    # 3b. Optional SQLite data sources (one seeded .db file per source).
    if args.sqlite_sources > 0:
        ds_dir = pathlib.Path(args.db_path).resolve().parent / "seed_sources"
        ds_dir.mkdir(parents=True, exist_ok=True)
        created_sources = []
        for i in range(1, args.sqlite_sources + 1):
            db_file = ds_dir / f"source_{i}.db"
            conn = sqlite3.connect(db_file)
            try:
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS orders_{i} ("
                    "id INTEGER PRIMARY KEY, customer TEXT, amount REAL, region TEXT, created_at TEXT)"
                )
                conn.execute(f"DELETE FROM orders_{i}")
                rows = [
                    (j, f"customer_{i}_{j}", round(10.0 * i + j, 2),
                     ["north", "south", "east", "west"][j % 4],
                     f"2026-0{(j % 6) + 1}-15")
                    for j in range(1, 51)
                ]
                conn.executemany(
                    f"INSERT INTO orders_{i} (id, customer, amount, region, created_at) VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
                conn.commit()
            finally:
                conn.close()

            ds_name = f"sqlite_source_{i}"
            r = client.post(
                "/api/data_sources",
                json={
                    "name": ds_name,
                    "type": "sqlite",
                    "config": {"database": str(db_file)},
                    "credentials": {},
                    "auth_policy": "system_only",
                    "generate_summary": False,
                    "generate_conversation_starters": False,
                    "generate_ai_rules": False,
                },
                headers=auth(admin_token, org_id),
            )
            ds_id = None
            if r.status_code == 200:
                ds_id = r.json().get("id")
            elif r.status_code in (400, 409) and "already exists" in r.text.lower():
                lr = client.get("/api/data_sources", headers=auth(admin_token, org_id))
                existing_ds = next((d for d in (lr.json() if lr.status_code == 200 else []) if d.get("name") == ds_name), None)
                ds_id = existing_ds["id"] if existing_ds else None
            if not ds_id:
                created_sources.append({"name": ds_name, "error": f"{r.status_code} {r.text}"})
                continue
            # Activate the seeded table — API-created sources index their
            # schema with is_active=False until tables are selected, and
            # create_data only targets active tables. Idempotent on reruns.
            ar = client.put(
                f"/api/data_sources/{ds_id}/update_tables_status",
                json={"activate": [f"orders_{i}"], "deactivate": []},
                headers=auth(admin_token, org_id),
            )
            created_sources.append({
                "name": ds_name, "id": ds_id, "db_file": str(db_file),
                "tables_activated": True if ar.status_code == 200 else f"{ar.status_code} {ar.text[:120]}",
            })
        summary["sqlite_sources"] = created_sources

    # 4. Optional members: invite via the org, then register with the token.
    members = []
    for email in args.invite:
        r = client.post(
            f"/api/organizations/{org_id}/members",
            json={"email": email, "role_id": "member"},
            headers=auth(admin_token, org_id),
        )
        if r.status_code != 200:
            members.append({"email": email, "error": f"invite failed: {r.status_code} {r.text}"})
            continue
        token = pending_invite_token(args.db_path, email)
        register(client, email.split("@")[0], email, args.password, invite_token=token)
        members.append({"email": email, "password": args.password, "token": login(client, email, args.password)})
    if members:
        summary["members"] = members

    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
