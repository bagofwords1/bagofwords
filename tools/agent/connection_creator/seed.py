#!/usr/bin/env python3
"""Seed the "connection creator" world into a running stack (real HTTP API).

    tools/agent/boot_stack.sh --dev
    cd backend && uv run python ../tools/agent/connection_creator/seed.py

World (password Password123! for everyone):
    admin@example.com      full admin; owns connection "Finance Warehouse" and a
                           PRIVATE agent "Finance Analyst" on it
    builder@example.com    role "Connection Builder" = [create_connections] only
    builder2@example.com   same role; owns connection "Marketing DB"
    connadmin@example.com  role "Connection Admin" = [manage_connections]
    member@example.com     plain member

Pass --before when the backend predates `create_connections`: the builders then
get a "Agent Builder" role (create_data_source) instead, which is the closest
thing that existed, and no peer connection is created.

Prints a JSON summary (ids + tokens) to stdout.
"""
import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import httpx  # noqa: E402
from seed_org import register, login, auth, pending_invite_token  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[3]
CHINOOK = ROOT / "backend" / "tests" / "config" / "chinook.sqlite"
PASSWORD = "Password123!"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--db-path", default=str(ROOT / "backend" / "db" / "agent.db"))
    p.add_argument("--before", action="store_true")
    args = p.parse_args()
    c = httpx.Client(base_url=args.base_url, timeout=60)

    register(c, "Ada Admin", "admin@example.com", PASSWORD)
    admin = login(c, "admin@example.com", PASSWORD)
    orgs = c.get("/api/organizations", headers=auth(admin)).json()
    if orgs:
        org = orgs[0]["id"]
    else:
        r = c.post("/api/organizations", json={"name": "Acme Analytics"}, headers=auth(admin))
        r.raise_for_status()
        org = r.json()["id"]
    H = auth(admin, org)

    def member(email, name):
        r = c.post(f"/api/organizations/{org}/members",
                   json={"organization_id": org, "email": email, "role": "member"}, headers=H)
        if r.status_code == 200:
            register(c, name, email, PASSWORD, invite_token=pending_invite_token(args.db_path, email))
        tok = login(c, email, PASSWORD)
        me = c.get("/api/users/whoami", headers=auth(tok)).json()
        return {"email": email, "token": tok, "user_id": me["id"]}

    def role(name, perms):
        existing = next((r for r in c.get(f"/api/organizations/{org}/roles", headers=H).json()
                         if r["name"] == name), None)
        if existing:
            return existing["id"]
        r = c.post(f"/api/organizations/{org}/roles", json={"name": name, "permissions": perms}, headers=H)
        if r.status_code != 200:
            sys.exit(f"create role {name} failed: {r.status_code} {r.text}")
        return r.json()["id"]

    def assign(role_id, user):
        c.post(f"/api/organizations/{org}/role-assignments",
               json={"role_id": role_id, "principal_type": "user", "principal_id": user["user_id"]}, headers=H)

    users = {
        "builder": member("builder@example.com", "Ben Builder"),
        "builder2": member("builder2@example.com", "Bea Builder"),
        "connadmin": member("connadmin@example.com", "Cora ConnAdmin"),
        "member": member("member@example.com", "Max Member"),
    }
    if args.before:
        b = role("Agent Builder", ["create_data_source"])
    else:
        b = role("Connection Builder", ["create_connections"])
        assign(role("Connection Admin", ["manage_connections"]), users["connadmin"])
    assign(b, users["builder"]); assign(b, users["builder2"])

    def connection(token, name):
        hit = next((x for x in c.get("/api/connections", headers=auth(token, org)).json() if x["name"] == name), None)
        if hit:
            return hit["id"]
        r = c.post("/api/connections", json={"name": name, "type": "sqlite",
                                             "config": {"database": str(CHINOOK)}, "credentials": {}},
                   headers=auth(token, org))
        if r.status_code != 200:
            sys.exit(f"create connection {name} failed: {r.status_code} {r.text}")
        return r.json()["id"]

    fin = connection(admin, "Finance Warehouse")
    agents = {a["name"]: a["id"] for a in c.get("/api/data_sources", headers=H).json()}
    if "Finance Analyst" not in agents:
        r = c.post("/api/data_sources", json={
            "name": "Finance Analyst", "connection_ids": [fin], "is_public": False,
            "generate_summary": False, "generate_conversation_starters": False, "generate_ai_rules": False,
        }, headers=H)
        r.raise_for_status()
    summary = {"org_id": org, "admin_token": admin, "finance_conn": fin,
               "users": {k: {"email": v["email"], "user_id": v["user_id"]} for k, v in users.items()}}
    if not args.before:
        summary["marketing_conn"] = connection(users["builder2"]["token"], "Marketing DB")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
