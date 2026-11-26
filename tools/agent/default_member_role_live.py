#!/usr/bin/env python3
"""Live sandbox: does the org's default member role reach people however they join?

Run from backend/ with its venv:

    uv run python ../tools/agent/default_member_role_live.py serve   # backend :8000, EE license faked
    uv run python ../tools/agent/default_member_role_live.py seed    # admin, custom default role, signup policy
    uv run python ../tools/agent/default_member_role_live.py join    # people join through 5 paths (real HTTP)
    uv run python ../tools/agent/default_member_role_live.py check   # each member's roles, as the admin sees them

`serve` boots the real app on a dedicated SQLite DB with an in-process fake
enterprise license (custom_roles, scim, domain_signup). Pair it with the
frontend (`cd frontend && yarn dev`, proxies /api -> :8000) for screenshots.

`seed` (real HTTP API), on a single-organization instance like the report:

    admin@example.com ....... org admin of "Acme Analytics"
    role-default-access ..... custom role (view_reports, create_reports),
                              set as the org's Default role on the Members page
    signup policy ........... domain acme-corp.example, no role override

`join` — nobody picks a role for any of these people:

    added@test.example ...... Add member button / API, then registers
    imported@test.example ... members CSV import, then registers
    carol@acme-corp.example . registers; admitted by the domain-signup policy
    dave@okta.example ....... provisioned by an identity provider over SCIM
    erin@okta.example ....... provisioned over SCIM as well (Okta push)
    control@test.example .... invited WITH an explicit "member" role (must stay member)

All passwords: Password123!
"""
import io
import os
import sys
from pathlib import Path

BACKEND = Path(os.environ.get("BOW_BACKEND_DIR") or Path(__file__).resolve().parents[2] / "backend").resolve()
DB = Path(os.environ.get("BOW_SANDBOX_DB") or Path(__file__).resolve().parents[2] / "backend" / "db" / "default_role_live.db").resolve()
PORT = int(os.environ.get("BOW_SANDBOX_PORT", "8000"))
BASE = f"http://127.0.0.1:{PORT}"
PASSWORD = "Password123!"
ORG_NAME = "Acme Analytics"
ROLE = "role-default-access"
DOMAIN = "acme-corp.example"
STATE = DB.with_suffix(".scim_token")


def serve():
    os.chdir(BACKEND)
    sys.path.insert(0, str(BACKEND))
    os.environ.setdefault("BOW_DATABASE_URL", f"sqlite:///{DB}")
    os.environ.setdefault("TEST_DATABASE_URL", f"sqlite:///{DB}")  # used when TESTING=true
    os.environ.setdefault("TESTING", "true")
    from app.ee import license as lic

    lic._get_license_key = lambda: "sandbox"
    lic._validate_license_key = lambda key: lic.LicenseInfo(
        licensed=True, tier="enterprise", org_name="default-role-sandbox",
        features=["custom_roles", "audit_logs", "scim", "domain_signup"],
    )
    lic.clear_license_cache()

    from alembic import command
    from alembic.config import Config
    command.upgrade(Config(str(BACKEND / "alembic.ini")), "head")

    from app.settings.config import settings
    flags = settings.bow_config.features
    flags.allow_uninvited_signups = True
    # Single-organization instance: the org admin owns the signup policy.
    flags.allow_multiple_organizations = False

    import uvicorn
    from main import app
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


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
    h = {"Authorization": f"Bearer {_login(c, 'admin@example.com')}"}
    org_id = _ok(c.get("/api/organizations", headers=h))[0]["id"]
    h["X-Organization-Id"] = org_id
    return org_id, h


def _register(c, email, name):
    _ok(c.post("/api/auth/register", json={"name": name, "email": email, "password": PASSWORD}), 200, 201)
    _login(c, email)  # first sign-in claims open invites


def seed():
    c = _client()
    c.post("/api/auth/register", json={"name": "Ada Admin", "email": "admin@example.com", "password": PASSWORD})
    h = {"Authorization": f"Bearer {_login(c, 'admin@example.com')}"}
    orgs = _ok(c.get("/api/organizations", headers=h))
    org_id = orgs[0]["id"]
    h["X-Organization-Id"] = org_id
    if orgs[0]["name"] != ORG_NAME:
        _ok(c.put("/api/organization", json={"name": ORG_NAME}, headers=h))
    c.put("/api/organization/onboarding", json={"dismissed": True}, headers=h)

    roles = {r["name"] for r in _ok(c.get(f"/api/organizations/{org_id}/roles", headers=h))}
    if ROLE not in roles:
        _ok(c.post(f"/api/organizations/{org_id}/roles",
                   json={"name": ROLE, "permissions": ["view_reports", "create_reports"]}, headers=h))
    _ok(c.put("/api/organization/default-member-role", json={"role": ROLE}, headers=h))
    # No auto_invite_role: domain signups follow the org default.
    _ok(c.put("/api/organization/signup-policy",
              json={"enabled": True, "allowed_domains": [DOMAIN]}, headers=h))
    tok = _ok(c.post("/api/enterprise/scim/tokens", json={"name": "Okta"}, headers=h), 200, 201)["token"]
    STATE.write_text(tok)
    print(f"seeded org {org_id}: default role = {ROLE}, signup domain = {DOMAIN}, SCIM token saved")


def join():
    from openpyxl import Workbook

    c = _client()
    org_id, h = _admin(c)

    # 1. Add member, no role picked (the modal's pre-fill path)
    _ok(c.post(f"/api/organizations/{org_id}/members",
               json={"organization_id": org_id, "email": "added@test.example"}, headers=h))
    _register(c, "added@test.example", "Added Person")

    # 2. CSV/XLSX import (the Import button)
    wb = Workbook()
    wb.active.append(["email", "note"])
    wb.active.append(["imported@test.example", "from import"])
    buf = io.BytesIO()
    wb.save(buf)
    _ok(c.post(f"/api/organizations/{org_id}/members/import?dry_run=false",
               files={"file": ("members.xlsx", buf.getvalue(),
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
               headers=h))
    _register(c, "imported@test.example", "Imported Person")

    # 3. Domain signup: just registers with a matching email
    _register(c, f"carol@{DOMAIN}", "Carol Domain")

    # 4/5. Identity provider pushes users over SCIM (what Okta provisioning does)
    scim = {"Authorization": f"Bearer {STATE.read_text().strip()}",
            "Content-Type": "application/scim+json"}
    for who in ("dave", "erin"):
        email = f"{who}@okta.example"
        _ok(c.post("/scim/v2/Users", headers=scim, json={
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": email, "active": True,
            "name": {"givenName": who.title(), "familyName": "Okta"},
            "emails": [{"value": email, "primary": True}],
        }), 200, 201)

    # Control: an admin who explicitly picks "member" gets member
    _ok(c.post(f"/api/organizations/{org_id}/members",
               json={"organization_id": org_id, "email": "control@test.example", "role": "member"}, headers=h))
    _register(c, "control@test.example", "Control Person")
    print("joined 6 people")


def check():
    c = _client()
    org_id, h = _admin(c)
    members = _ok(c.get(f"/api/organizations/{org_id}/members", headers=h))
    print(f"{'member':<28} {'roles (Members page)':<32} via")
    via = {"added@test.example": "Add member (no role)", "imported@test.example": "CSV import",
           f"carol@{DOMAIN}": "domain signup", "dave@okta.example": "SCIM (Okta)",
           "erin@okta.example": "SCIM (Okta)", "control@test.example": "Add member, role=member (control)"}
    bad = 0
    for m in members:
        email = (m.get("user") or {}).get("email") or m.get("email")
        if email not in via:
            continue
        names = sorted(r["name"] for r in (m.get("roles") or []))
        want = ["member"] if email.startswith("control@") else [ROLE]
        mark = "OK " if names == want else "BAD"
        bad += names != want
        print(f"{mark} {email:<24} {', '.join(names) or '(none)':<32} {via[email]}")
    print("RESULT:", "all people got the expected role" if not bad else f"{bad} member(s) got the wrong role")


if __name__ == "__main__":
    {"serve": serve, "seed": seed, "join": join, "check": check}[sys.argv[1]]()
