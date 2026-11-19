#!/usr/bin/env python3
"""Live sandbox for inviting a member with roles and groups.

Run from backend/ with its venv:

    uv run python ../tools/agent/invite_access_live.py serve   # backend :8000, EE license faked
    uv run python ../tools/agent/invite_access_live.py seed    # seed the scenario below
    uv run python ../tools/agent/invite_access_live.py check EMAIL
                                                              # what EMAIL holds in the org

    BOW_BACKEND_DIR=<other checkout>/backend ... serve        # "before" run on another build

`serve` boots the real app on a dedicated SQLite DB with an in-process fake
enterprise license (custom_roles, so custom roles and groups work without a real
key). Pair it with the frontend (`cd frontend && yarn dev`, proxies /api -> :8000).

`seed` builds, through the real HTTP API:

    admin@example.com ...... org admin of "Acme Analytics"
    custom roles ........... Analyst (view_members, create_reports),
                             Data Steward (manage_instructions, manage_entities)
    groups ................. Finance, Marketing, Data Platform
    fiona@, mark@ .......... registered members
    dana@other.example ..... has an account in ANOTHER org only — inviting her
                             here is the "existing account" path
All passwords: Password123!
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(os.environ.get("BOW_BACKEND_DIR") or Path(__file__).resolve().parents[2] / "backend").resolve()
DB = Path(os.environ.get("BOW_SANDBOX_DB") or Path(__file__).resolve().parents[2] / "backend" / "db" / "invite_access_live.db").resolve()
PORT = int(os.environ.get("BOW_SANDBOX_PORT", "8000"))
BASE = f"http://127.0.0.1:{PORT}"
PASSWORD = "Password123!"
ORG_NAME = "Acme Analytics"


def serve():
    os.chdir(BACKEND)
    sys.path.insert(0, str(BACKEND))
    os.environ.setdefault("BOW_DATABASE_URL", f"sqlite:///{DB}")
    os.environ.setdefault("TEST_DATABASE_URL", f"sqlite:///{DB}")  # used when TESTING=true
    os.environ.setdefault("TESTING", "true")
    from app.ee import license as lic

    lic._get_license_key = lambda: "sandbox"
    lic._validate_license_key = lambda key: lic.LicenseInfo(
        licensed=True, tier="enterprise", org_name="invite-sandbox",
        features=["custom_roles", "audit_logs"],
    )
    lic.clear_license_cache()

    from alembic import command
    from alembic.config import Config
    command.upgrade(Config(str(BACKEND / "alembic.ini")), "head")

    from app.settings.config import settings
    flags = settings.bow_config.features
    flags.allow_uninvited_signups = True
    flags.allow_multiple_organizations = True

    import uvicorn
    from main import app
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


def _invite_token(email):
    con = sqlite3.connect(str(DB))
    try:
        row = con.execute(
            "SELECT invite_token FROM memberships WHERE email = ? AND user_id IS NULL "
            "ORDER BY created_at DESC LIMIT 1", (email,),
        ).fetchone()
    finally:
        con.close()
    return row[0] if row else None


def _client():
    import httpx
    return httpx.Client(base_url=BASE, timeout=120)


def _ok(r, *codes):
    if r.status_code not in (codes or (200, 201, 204)):
        sys.exit(f"{r.request.method} {r.request.url} -> {r.status_code} {r.text}")
    return r.json() if r.content else None


def _login(c, email):
    return _ok(c.post("/api/auth/jwt/login", data={"username": email, "password": PASSWORD}))["access_token"]


def _admin(c):
    tok = _login(c, "admin@example.com")
    h = {"Authorization": f"Bearer {tok}"}
    org_id = _ok(c.get("/api/organizations", headers=h))[0]["id"]
    h["X-Organization-Id"] = org_id
    return org_id, h


def seed():
    c = _client()
    c.post("/api/auth/register", json={"name": "Ada Admin", "email": "admin@example.com", "password": PASSWORD})
    tok = _login(c, "admin@example.com")
    h = {"Authorization": f"Bearer {tok}"}
    # The first user on a clean DB gets an auto-created org; reuse and rename it
    # so the admin has exactly one org and the UI lands on it.
    orgs = _ok(c.get("/api/organizations", headers=h))
    org_id = orgs[0]["id"] if orgs else _ok(c.post("/api/organizations", json={"name": ORG_NAME}, headers=h))["id"]
    h["X-Organization-Id"] = org_id
    if orgs and orgs[0]["name"] != ORG_NAME:
        _ok(c.put("/api/organization", json={"name": ORG_NAME}, headers=h))
    c.put("/api/organization/onboarding", json={"dismissed": True}, headers=h)

    roles = {r["name"]: r for r in _ok(c.get(f"/api/organizations/{org_id}/roles", headers=h))}
    for name, perms in (("analyst", ["view_members", "create_reports"]),
                        ("data steward", ["manage_instructions", "manage_entities"])):
        if name not in roles:
            roles[name] = _ok(c.post(f"/api/organizations/{org_id}/roles",
                                     json={"name": name, "permissions": perms}, headers=h))

    groups = {g["name"]: g for g in _ok(c.get(f"/api/organizations/{org_id}/groups", headers=h))}
    for gname in ("Finance", "Marketing", "Data Platform"):
        if gname not in groups:
            groups[gname] = _ok(c.post(f"/api/organizations/{org_id}/groups", json={"name": gname}, headers=h))

    for key, name in (("fiona", "Fiona Finance"), ("mark", "Mark Marketing")):
        email = f"{key}@example.com"
        c.post(f"/api/organizations/{org_id}/members",
               json={"organization_id": org_id, "email": email, "role": "member"}, headers=h)
        body = {"name": name, "email": email, "password": PASSWORD}
        token = _invite_token(email)
        if token:
            body["invite_token"] = token
        c.post("/api/auth/register", json=body)

    # Dana: an account that exists, but only in another org.
    c.post("/api/auth/register", json={"name": "Dana Elsewhere", "email": "dana@other.example", "password": PASSWORD})
    dtok = _login(c, "dana@other.example")
    dh = {"Authorization": f"Bearer {dtok}"}
    if not _ok(c.get("/api/organizations", headers=dh)):
        _ok(c.post("/api/organizations", json={"name": "Other Co"}, headers=dh))

    print(json.dumps({"org_id": org_id, "roles": sorted(roles), "groups": sorted(groups)}, indent=2))


def check(email):
    """Print the roles, permissions and groups EMAIL holds in the org, from the admin's view."""
    c = _client()
    org_id, h = _admin(c)
    members = _ok(c.get(f"/api/organizations/{org_id}/members", headers=h))
    m = next((m for m in members if (m.get("email") or (m.get("user") or {}).get("email")) == email), None)
    if m is None:
        print(json.dumps({"email": email, "member": False}))
        return
    groups = _ok(c.get(f"/api/organizations/{org_id}/groups", headers=h))
    uid = m.get("user_id")
    in_groups = sorted(g["name"] for g in groups
                       if (uid and uid in g["member_user_ids"]) or m["id"] in g["member_membership_ids"])
    out = {"email": email, "pending": uid is None, "legacy_role": m["role"],
           "roles": sorted(r["name"] for r in m.get("roles") or []), "groups": in_groups}
    if uid:  # what the person actually gets
        try:
            tok = _login(c, email)
            me = _ok(c.get("/api/users/whoami", headers={"Authorization": f"Bearer {tok}"}))
            o = next(o for o in me["organizations"] if o["id"] == org_id)
            out["effective_roles"] = sorted(o.get("roles") or [])
        except SystemExit:
            pass
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "check":
        check(sys.argv[2])
    else:
        {"serve": serve, "seed": seed}[cmd]()
