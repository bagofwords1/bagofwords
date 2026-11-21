#!/usr/bin/env python3
"""Live sandbox for group managers (delegated admin scoped to one group).

Run from backend/ with its venv:

    uv run python ../tools/agent/group_manager_live.py serve    # backend :8000, EE license faked
    uv run python ../tools/agent/group_manager_live.py seed     # seed the scenario below
    uv run python ../tools/agent/group_manager_live.py attack   # try to break it over HTTP
    uv run python ../tools/agent/group_manager_live.py reset    # restore the seeded Sales roster
    uv run python ../tools/agent/group_manager_live.py check EMAIL

`serve` boots the real app on a dedicated SQLite DB with an in-process fake
enterprise license (custom_roles + audit_logs). Pair it with the frontend
(`cd frontend && yarn dev`, proxies /api -> :8000).

`seed` builds, through the real HTTP API only:

    admin@example.com ...... org admin of "Acme Analytics"
    lena@ .................. member of Sales — promoted to Sales manager
    pete@ .................. member of Sales
    sam@ ................... member of Sales (the one Lena removes in the UI flow)
    fin@ ................... member of Finance
    olga@ .................. org member in no group
    llm@, audit@, settings@, sa@
                             hold one admin permission each (manage_llm,
                             view_audit_logs, manage_settings, manage_service_accounts)
    groups ................. Sales, Finance (hand-managed), Entra Analysts (imported, oidc)
    agent .................. "Sales pipeline" (private, chinook) in folder "Sales";
                             the Sales group can query the folder
All passwords: Password123!

`attack` runs every escalation we could think of with real sessions and prints
a table of expected vs actual status codes; it exits 1 on any mismatch. It
restores the seeded state as it goes, so it can be re-run.
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = Path(os.environ.get("BOW_BACKEND_DIR") or ROOT / "backend").resolve()
DB = Path(os.environ.get("BOW_SANDBOX_DB") or ROOT / "backend" / "db" / "group_manager_live.db").resolve()
PORT = int(os.environ.get("BOW_SANDBOX_PORT", "8000"))
BASE = f"http://127.0.0.1:{PORT}"
PASSWORD = "Password123!"
ORG_NAME = "Acme Analytics"
CHINOOK = ROOT / "backend" / "tests" / "config" / "chinook.sqlite"
PEOPLE = {"lena": "Lena Lead", "pete": "Pete Peer", "sam": "Sam Sales", "fin": "Fin Finance", "olga": "Olga Outside",
          "llm": "Lio LLM", "audit": "Ari Audit", "settings": "Sia Settings", "sa": "Sol Service"}
# Single-permission admins, for checking where the Admin entry opens.
ADMIN_ROLES = {"llm": ["manage_llm"], "audit": ["view_audit_logs"], "settings": ["manage_settings"],
               "sa": ["manage_service_accounts"]}


def serve():
    os.chdir(BACKEND)
    sys.path.insert(0, str(BACKEND))
    os.environ.setdefault("BOW_DATABASE_URL", f"sqlite:///{DB}")
    os.environ.setdefault("TEST_DATABASE_URL", f"sqlite:///{DB}")  # used when TESTING=true
    os.environ.setdefault("TESTING", "true")
    from app.ee import license as lic

    lic._get_license_key = lambda: "sandbox"
    lic._validate_license_key = lambda key: lic.LicenseInfo(
        licensed=True, tier="enterprise", org_name="group-manager-sandbox",
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


# ── HTTP helpers ─────────────────────────────────────────────────────────

def _client():
    import httpx
    return httpx.Client(base_url=BASE, timeout=120)


def _ok(r, *codes):
    if r.status_code not in (codes or (200, 201, 204)):
        sys.exit(f"{r.request.method} {r.request.url} -> {r.status_code} {r.text}")
    return r.json() if r.content else None


def _login(c, email):
    return _ok(c.post("/api/auth/jwt/login", data={"username": email, "password": PASSWORD}))["access_token"]


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


def _ctx(c):
    """Resolve the org, every seeded person (headers + ids) and the groups."""
    tok = _login(c, "admin@example.com")
    org_id = _ok(c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}))[0]["id"]

    def h(token):
        return {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}

    people = {"admin": {"h": h(tok)}}
    members = _ok(c.get(f"/api/organizations/{org_id}/members", headers=people["admin"]["h"]))
    for key in ["admin", *PEOPLE]:
        email = f"{key}@example.com"
        m = next(m for m in members if (m.get("email") or (m.get("user") or {}).get("email")) == email)
        people.setdefault(key, {})
        people[key].update(email=email, user_id=m.get("user_id"), membership_id=m["id"])
        if key != "admin":
            people[key]["h"] = h(_login(c, email))
    groups = {g["name"]: g for g in _ok(c.get(f"/api/organizations/{org_id}/groups", headers=people["admin"]["h"]))}
    return org_id, people, groups


# ── seed ─────────────────────────────────────────────────────────────────

def seed():
    c = _client()
    c.post("/api/auth/register", json={"name": "Ada Admin", "email": "admin@example.com", "password": PASSWORD})
    tok = _login(c, "admin@example.com")
    h = {"Authorization": f"Bearer {tok}"}
    orgs = _ok(c.get("/api/organizations", headers=h))
    org_id = orgs[0]["id"] if orgs else _ok(c.post("/api/organizations", json={"name": ORG_NAME}, headers=h))["id"]
    h["X-Organization-Id"] = org_id
    if orgs and orgs[0]["name"] != ORG_NAME:
        _ok(c.put("/api/organization", json={"name": ORG_NAME}, headers=h))
    c.put("/api/organization/onboarding", json={"dismissed": True}, headers=h)

    for key, name in PEOPLE.items():
        email = f"{key}@example.com"
        c.post(f"/api/organizations/{org_id}/members",
               json={"organization_id": org_id, "email": email, "role": "member"}, headers=h)
        body = {"name": name, "email": email, "password": PASSWORD}
        token = _invite_token(email)
        if token:
            body["invite_token"] = token
        c.post("/api/auth/register", json=body)

    _, people, groups = _ctx(c)
    for gname in ("Sales", "Finance"):
        if gname not in groups:
            groups[gname] = _ok(c.post(f"/api/organizations/{org_id}/groups", json={"name": gname}, headers=h))
    for who, gname in (("lena", "Sales"), ("pete", "Sales"), ("sam", "Sales"), ("fin", "Finance")):
        c.post(f"/api/organizations/{org_id}/groups/{groups[gname]['id']}/members",
               json={"user_id": people[who]["user_id"]}, headers=h)

    # An imported group: groups only become imported through a directory sync,
    # which this sandbox has no IdP for, so mark one the way the OIDC sync does.
    if "Entra Analysts" not in groups:
        g = _ok(c.post(f"/api/organizations/{org_id}/groups", json={"name": "Entra Analysts"}, headers=h))
        c.post(f"/api/organizations/{org_id}/groups/{g['id']}/members",
               json={"user_id": people["olga"]["user_id"]}, headers=h)
        con = sqlite3.connect(str(DB))
        con.execute("UPDATE groups SET external_provider='oidc', external_id='entra-analysts' WHERE id=?", (g["id"],))
        con.commit()
        con.close()

    folders = {f["name"]: f for f in _ok(c.get("/api/folders", headers=h))["folders"]}
    folder_id = folders["Sales"]["id"] if "Sales" in folders else _ok(
        c.post("/api/folders", json={"name": "Sales"}, headers=h))["id"]
    agents = {d["name"]: d for d in _ok(c.get("/api/data_sources", headers=h))}
    if "Sales pipeline" not in agents:
        ds = _ok(c.post("/api/data_sources", headers=h, json={
            "name": "Sales pipeline", "type": "sqlite", "config": {"database": str(CHINOOK)},
            "credentials": {}, "auth_policy": "system_only", "generate_summary": False,
            "generate_conversation_starters": False, "generate_ai_rules": False,
        }))
        _ok(c.put(f"/api/data_sources/{ds['id']}", json={"is_public": False}, headers=h))
        _ok(c.put(f"/api/folders/items/agent/{ds['id']}/placement", json={"parent_id": folder_id}, headers=h))
    _ok(c.put(f"/api/folders/{folder_id}/share", headers=h,
              json={"principal_type": "group", "principal_id": groups["Sales"]["id"], "role": "Viewer"}))

    _ok(c.put(f"/api/organizations/{org_id}/groups/{groups['Sales']['id']}/managers/{people['lena']['user_id']}",
              headers=h))
    roles = {r["name"]: r for r in _ok(c.get(f"/api/organizations/{org_id}/roles", headers=h))}
    for who, perms in ADMIN_ROLES.items():
        name = f"only {perms[0]}"
        role = roles.get(name) or _ok(c.post(f"/api/organizations/{org_id}/roles", headers=h,
                                             json={"name": name, "permissions": ["view_members", *perms]}))
        c.post(f"/api/organizations/{org_id}/role-assignments", headers=h,
               json={"role_id": role["id"], "principal_type": "user", "principal_id": people[who]["user_id"]})
    print(json.dumps({"org_id": org_id, "groups": sorted(groups), "sales_manager": "lena@example.com"}, indent=2))


# ── attack ───────────────────────────────────────────────────────────────

def attack():
    c = _client()
    org_id, p, groups = _ctx(c)
    admin = p["admin"]["h"]
    sales, finance, imported = (groups[n]["id"] for n in ("Sales", "Finance", "Entra Analysts"))
    base = f"/api/organizations/{org_id}"
    rows, failures = [], 0

    def run(label, method, path, actor, expect, body=None):
        nonlocal failures
        r = c.request(method, path, headers=p[actor]["h"], json=body)
        ok = r.status_code in (expect if isinstance(expect, tuple) else (expect,))
        failures += not ok
        rows.append((label, actor, method, path.replace(base, ""), expect, r.status_code, "PASS" if ok else "FAIL"))
        return r

    def members(gid):
        return {m["user_id"] for m in _ok(c.get(f"{base}/groups/{gid}/members", headers=admin)) if m.get("user_id")}

    def managers(gid):
        return set(next(g for g in _ok(c.get(f"{base}/groups", headers=admin)) if g["id"] == gid)["manager_user_ids"])

    def uid(who):
        return p[who]["user_id"]

    assert managers(sales) == {uid("lena")}, "seed: Lena should manage Sales"

    # Allowed for a manager
    run("manager lists own group", "GET", f"{base}/groups/{sales}/members", "lena", 200)
    run("manager adds an org member", "POST", f"{base}/groups/{sales}/members", "lena", 201, {"user_id": uid("olga")})
    run("manager removes a member", "DELETE", f"{base}/groups/{sales}/members/{uid('olga')}", "lena", 204)

    # Plain member
    run("member lists group", "GET", f"{base}/groups/{sales}/members", "pete", 403)
    run("member adds to own group", "POST", f"{base}/groups/{sales}/members", "pete", 403, {"user_id": uid("olga")})
    run("member promotes self", "PUT", f"{base}/groups/{sales}/managers/{uid('pete')}", "pete", 403)

    # Manager outside their group
    run("manager lists other group", "GET", f"{base}/groups/{finance}/members", "lena", 403)
    run("manager adds self to other group", "POST", f"{base}/groups/{finance}/members", "lena", 403, {"user_id": uid("lena")})
    run("manager removes from other group", "DELETE", f"{base}/groups/{finance}/members/{uid('fin')}", "lena", 403)
    run("manager promotes in other group", "PUT", f"{base}/groups/{finance}/managers/{uid('fin')}", "lena", 403)

    # Promotion rules
    run("manager promotes non-member", "PUT", f"{base}/groups/{sales}/managers/{uid('olga')}", "lena", 422)
    run("manager promotes random id", "PUT", f"{base}/groups/{sales}/managers/00000000-0000-0000-0000-000000000000", "lena", 422)
    run("manager promotes a member", "PUT", f"{base}/groups/{sales}/managers/{uid('pete')}", "lena", 204)
    run("manager demotes peer manager", "DELETE", f"{base}/groups/{sales}/managers/{uid('pete')}", "lena", 403)
    run("manager removes peer manager", "DELETE", f"{base}/groups/{sales}/members/{uid('pete')}", "lena", 403)
    run("manager demotes self", "DELETE", f"{base}/groups/{sales}/managers/{uid('lena')}", "lena", 403)
    run("admin demotes manager", "DELETE", f"{base}/groups/{sales}/managers/{uid('pete')}", "admin", 204)

    # The group itself and its access
    run("manager renames group", "PUT", f"{base}/groups/{sales}", "lena", 403, {"name": "pwned"})
    run("manager deletes group", "DELETE", f"{base}/groups/{sales}", "lena", 403)
    run("manager creates group", "POST", f"{base}/groups", "lena", 403, {"name": "mine"})
    roles = _ok(c.get(f"{base}/roles", headers=admin))
    admin_role = next(r for r in roles if r["name"] == "admin")
    run("manager gives group admin role", "POST", f"{base}/role-assignments", "lena", 403,
        {"role_id": admin_role["id"], "principal_type": "group", "principal_id": sales})
    run("manager gives self admin role", "POST", f"{base}/role-assignments", "lena", 403,
        {"role_id": admin_role["id"], "principal_type": "user", "principal_id": uid("lena")})
    run("manager invites new user", "POST", f"{base}/members", "lena", 403,
        {"organization_id": org_id, "email": "new@example.com", "role": "member"})

    # Minting the grant another way
    run("admin mints group grant (user)", "POST", f"{base}/resource-grants", "admin", 422,
        {"resource_type": "group", "resource_id": finance, "principal_type": "user",
         "principal_id": uid("fin"), "permissions": ["manage_members"]})
    run("admin mints group grant (group)", "POST", f"{base}/resource-grants", "admin", 422,
        {"resource_type": "group", "resource_id": finance, "principal_type": "group",
         "principal_id": finance, "permissions": ["manage_members"]})
    run("manager mints group grant", "POST", f"{base}/resource-grants", "lena", (403, 422),
        {"resource_type": "group", "resource_id": finance, "principal_type": "user",
         "principal_id": uid("lena"), "permissions": ["manage_members"]})
    custom = _ok(c.post(f"{base}/roles", headers=admin, json={"name": f"probe-{os.getpid()}", "permissions": []}))
    run("role carries group grant", "PUT", f"{base}/roles/{custom['id']}", "admin", 422,
        {"resource_grants": [{"resource_type": "group", "resource_id": sales, "permissions": ["manage_members"]}]})
    c.delete(f"{base}/roles/{custom['id']}", headers=admin)

    # Imported groups
    run("admin promotes in imported group", "PUT", f"{base}/groups/{imported}/managers/{uid('olga')}", "admin", 409)

    # Mass assignment / foreign ids
    run("extra fields ignored on add", "POST", f"{base}/groups/{sales}/members", "lena", 201,
        {"user_id": uid("olga"), "is_manager": True, "permissions": ["full_admin_access"]})
    run("…and did not promote", "GET", f"{base}/groups/{sales}/members", "lena", 200)
    assert uid("olga") not in managers(sales), "mass assignment promoted olga"
    run("cleanup", "DELETE", f"{base}/groups/{sales}/members/{uid('olga')}", "lena", 204)
    run("foreign group id", "GET", f"{base}/groups/11111111-1111-1111-1111-111111111111/members", "lena", (403, 404))

    # Leaving ends the role, rejoining doesn't restore it
    run("manager leaves group", "DELETE", f"{base}/groups/{sales}/members/{uid('lena')}", "lena", 204)
    run("ex-manager lists group", "GET", f"{base}/groups/{sales}/members", "lena", 403)
    run("admin re-adds ex-manager", "POST", f"{base}/groups/{sales}/members", "admin", 201, {"user_id": uid("lena")})
    run("re-added is not manager", "POST", f"{base}/groups/{sales}/members", "lena", 403, {"user_id": uid("olga")})
    run("admin restores manager", "PUT", f"{base}/groups/{sales}/managers/{uid('lena')}", "admin", 204)

    w = max(len(r[0]) for r in rows)
    for label, actor, method, path, expect, got, verdict in rows:
        print(f"{verdict}  {label:<{w}}  {actor:<5} {method:<6} {path:<70} expect={expect} got={got}")
    print(f"\n{len(rows) - failures}/{len(rows)} as expected")
    assert members(sales) >= {uid("lena"), uid("pete"), uid("sam")}
    sys.exit(1 if failures else 0)


def reset():
    """Back to the seeded Sales roster: lena (manager), pete, sam."""
    c = _client()
    org_id, p, groups = _ctx(c)
    h, sales, base = p["admin"]["h"], groups["Sales"]["id"], f"/api/organizations/{org_id}"
    current = {m["user_id"] for m in _ok(c.get(f"{base}/groups/{sales}/members", headers=h)) if m.get("user_id")}
    for who in ("lena", "pete", "sam"):
        if p[who]["user_id"] not in current:
            _ok(c.post(f"{base}/groups/{sales}/members", json={"user_id": p[who]["user_id"]}, headers=h))
    for who in ("olga", "fin"):
        if p[who]["user_id"] in current:
            _ok(c.delete(f"{base}/groups/{sales}/members/{p[who]['user_id']}", headers=h))
    _ok(c.delete(f"{base}/groups/{sales}/managers/{p['pete']['user_id']}", headers=h))
    _ok(c.put(f"{base}/groups/{sales}/managers/{p['lena']['user_id']}", headers=h))
    print("reset")


def check(email):
    c = _client()
    org_id, p, groups = _ctx(c)
    key = email.split("@")[0]
    me = _ok(c.get("/api/users/whoami", headers=p[key]["h"]))
    o = next(o for o in me["organizations"] if o["id"] == org_id)
    names = {g["id"]: n for n, g in groups.items()}
    out = {
        "email": email,
        "groups": sorted(n for n, g in groups.items() if p[key]["user_id"] in g["member_user_ids"]),
        "manages": sorted(names.get(k.split(":", 1)[1], k) for k in (o.get("resource_permissions") or {}) if k.startswith("group:")),
        "agents": sorted(d["name"] for d in _ok(c.get("/api/data_sources/active", headers=p[key]["h"]))),
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "check":
        check(sys.argv[2])
    else:
        {"serve": serve, "seed": seed, "attack": attack, "reset": reset}[cmd]()
