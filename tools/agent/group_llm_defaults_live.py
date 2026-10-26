#!/usr/bin/env python3
"""Live sandbox for group-level default LLM models.

Two subcommands, run from backend/ with its venv:

    uv run python ../tools/agent/group_llm_defaults_live.py serve   # backend on :8000, EE license faked
    uv run python ../tools/agent/group_llm_defaults_live.py seed    # seed the two-group scenario

    BOW_SANDBOX_PORT=8001 ... serve / seed --legacy   # "before" run on a build without the feature

`serve` boots the real app on a dedicated SQLite DB with an in-process fake
enterprise license (custom_roles + llm_access_control), so groups and model
access work without a real key. Pair it with the frontend (`cd frontend &&
yarn dev`, which proxies /api to :8000).

`seed` builds the scenario through the real HTTP API:

    org default ............ the provider's catalog default (open to all)
    Finance  (group 1) ..... default Claude Opus 5.5   (restricted)
    Marketing (group 2) .... default GPT-6 Sol         (restricted)
    fiona@  → Finance   mark@ → Marketing   bianca@ → both   nate@ → no group

and prints, for every user, the default model and the models they can see.
All passwords: Password123!
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "backend"
DB = BACKEND / "db" / "group_llm_defaults_live.db"
PORT = int(os.environ.get("BOW_SANDBOX_PORT", "8000"))
BASE = f"http://127.0.0.1:{PORT}"
# --legacy: seed the closest pre-feature setup (group *grants* instead of group
# defaults) for "before" evidence against a build without group defaults.
LEGACY = "--legacy" in sys.argv
PASSWORD = "Password123!"


def serve():
    os.chdir(BACKEND)
    sys.path.insert(0, str(BACKEND))
    os.environ.setdefault("BOW_DATABASE_URL", f"sqlite:///{DB}")
    os.environ.setdefault("TEST_DATABASE_URL", f"sqlite:///{DB}")  # used when TESTING=true
    os.environ.setdefault("TESTING", "true")
    from app.ee import license as lic

    features = ["custom_roles", "llm_access_control", "audit_logs", "model_routing", "llm_fallback"]
    lic._get_license_key = lambda: "sandbox"
    lic._validate_license_key = lambda key: lic.LicenseInfo(
        licensed=True, tier="enterprise", org_name="group-llm-sandbox", features=features,
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


def seed():
    import httpx
    c = httpx.Client(base_url=BASE, timeout=120)

    def ok(r, *codes):
        if r.status_code not in (codes or (200, 201, 204)):
            sys.exit(f"{r.request.method} {r.request.url} -> {r.status_code} {r.text}")
        return r.json() if r.content else None

    def login(email):
        r = c.post("/api/auth/jwt/login", data={"username": email, "password": PASSWORD})
        return ok(r)["access_token"]

    # Admin + org
    admin_email = "admin@example.com"
    c.post("/api/auth/register", json={"name": "Ada Admin", "email": admin_email, "password": PASSWORD})
    tok = login(admin_email)
    h = {"Authorization": f"Bearer {tok}"}
    orgs = ok(c.get("/api/organizations", headers=h))
    if orgs:
        org_id = orgs[0]["id"]
    else:
        org_id = ok(c.post("/api/organizations", json={"name": "Acme Analytics"}, headers=h))["id"]
    h["X-Organization-Id"] = org_id
    c.put("/api/organization/onboarding", json={"dismissed": True}, headers=h)

    # Providers (dummy keys: provider creation does not test the connection)
    existing = {p["provider_type"] for p in ok(c.get("/api/llm/providers", headers=h))}
    for ptype, name in (("anthropic", "Anthropic"), ("openai", "OpenAI")):
        if ptype not in existing:
            ok(c.post("/api/llm/providers", json={
                "name": name, "provider_type": ptype, "credentials": {"api_key": "sk-sandbox"}, "models": [],
            }, headers=h))
    models = ok(c.get("/api/llm/models", headers=h))
    by_name = {m["name"]: m for m in models}
    keep = {"Claude Opus 5.5", "Claude Sonnet 5.5", "Claude Haiku 5.5", "GPT-6 Sol", "GPT-6 Luna"}
    org_default = next(m for m in models if m["is_default"])
    org_small = next((m for m in models if m["is_small_default"]), None)
    keep |= {org_default["name"]} | ({org_small["name"]} if org_small else set())
    for m in models:  # keep the screenshot table short
        want = m["name"] in keep
        if m["is_enabled"] != want and not (m["is_default"] or m["is_small_default"]):
            ok(c.post(f"/api/llm/models/{m['id']}/toggle", params={"enabled": want}, headers=h))
    opus, sol = by_name["Claude Opus 5.5"]["id"], by_name["GPT-6 Sol"]["id"]

    # Users
    users = {}
    for key, name in (("fiona", "Fiona Finance"), ("mark", "Mark Marketing"),
                      ("bianca", "Bianca Both"), ("nate", "Nate NoGroup")):
        email = f"{key}@example.com"
        c.post(f"/api/organizations/{org_id}/members", json={"organization_id": org_id, "email": email, "role": "member"}, headers=h)
        token = _invite_token(email)
        body = {"name": name, "email": email, "password": PASSWORD}
        if token:
            body["invite_token"] = token
        c.post("/api/auth/register", json=body)
        utok = login(email)
        me = ok(c.get("/api/users/whoami", headers={"Authorization": f"Bearer {utok}"}))
        users[key] = {"email": email, "token": utok, "id": me["id"], "name": name}

    # Groups
    groups = {g["name"]: g["id"] for g in ok(c.get(f"/api/organizations/{org_id}/groups", headers=h))}
    for gname in ("Finance", "Marketing"):
        if gname not in groups:
            groups[gname] = ok(c.post(f"/api/organizations/{org_id}/groups", json={"name": gname}, headers=h))["id"]
    for key, gnames in (("fiona", ["Finance"]), ("mark", ["Marketing"]), ("bianca", ["Finance", "Marketing"])):
        for g in gnames:
            c.post(f"/api/organizations/{org_id}/groups/{groups[g]}/members", json={"user_id": users[key]["id"]}, headers=h)

    # Restrict each group's model and make it that group's default
    for mid in (opus, sol):
        ok(c.put(f"/api/llm/models/{mid}/restricted", json={"is_restricted": True}, headers=h))
    for gname, mid in (("Finance", opus), ("Marketing", sol)):
        if LEGACY:
            c.post(f"/api/llm/models/{mid}/access", json={"principal_type": "group", "principal_id": groups[gname]}, headers=h)
        else:
            ok(c.put(f"/api/llm/group-defaults/{groups[gname]}", json={"default_llm_model_id": mid}, headers=h))

    # Report what each person gets
    report = {"org_id": org_id, "org_default": org_default["name"], "users": {}}
    for key, u in users.items():
        uh = {"Authorization": f"Bearer {u['token']}", "X-Organization-Id": org_id}
        ms = ok(c.get("/api/llm/models", params={"is_enabled": True}, headers=uh))
        d = next((m for m in ms if m.get("is_inherited_default")), None) or next((m for m in ms if m["is_default"]), None)
        report["users"][u["email"]] = {
            "default": d and d["name"],
            "source": d and ((d.get("inherited_default_source") or "org") + (f":{d['inherited_default_group_name']}" if d.get("inherited_default_group_name") else "")),
            "visible": sorted(m["name"] for m in ms),
        }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    {"serve": serve, "seed": seed}[sys.argv[1] if len(sys.argv) > 1 else "serve"]()
