#!/usr/bin/env python3
"""Live sandbox for delegated LLM management: provider managers, group managers
picking their group's models, and the decision model following model access.

Run from backend/ with its venv:

    uv run python ../tools/agent/llm_delegation_live.py serve    # backend :8000, EE license faked
    uv run python ../tools/agent/llm_delegation_live.py seed     # seed the scenario below
    uv run python ../tools/agent/llm_delegation_live.py attack   # HTTP attack table, real sessions
    uv run python ../tools/agent/llm_delegation_live.py check    # what each person gets

`serve` boots the real app on its own SQLite DB with an in-process fake
enterprise license. Pair it with the frontend (`cd frontend && yarn dev`).

The scenario (all passwords Password123!):

    providers
      Anthropic ............ org default Claude Sonnet 5.5, small default Claude Haiku 5.5 (open to all)
      OpenAI (Finance) ..... GPT-6 Sol, GPT-6 Luna, decision model GPT-6 Luna Decisions — restricted,
                             granted to Finance; GPT-6.1 Terra switched off
      Gemini (Marketing) ... Gemini 4 Argon, Gemini 3.6 Flash — restricted, granted to Marketing;
                             Gemini 3.5 Flash-Lite switched off
    groups
      Finance .............. alice@ (group manager), fiona@, bianca@
      Marketing ............ bob@ (group manager), mark@, bianca@
    provider managers
      alice@ → OpenAI (Finance)      bob@ → Gemini (Marketing)
    nate@ ................. in no group
    Auto router ........... on, decision method, decision model GPT-6 Luna Decisions (Finance only)
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "backend"
DB = BACKEND / "db" / "llm_delegation_live.db"
PORT = int(os.environ.get("BOW_SANDBOX_PORT", "8000"))
BASE = f"http://127.0.0.1:{PORT}"
PASSWORD = "Password123!"
FEATURES = ["custom_roles", "llm_access_control", "audit_logs", "model_routing", "llm_fallback"]


def serve():
    os.chdir(BACKEND)
    sys.path.insert(0, str(BACKEND))
    (BACKEND / "db").mkdir(exist_ok=True)
    os.environ.setdefault("BOW_DATABASE_URL", f"sqlite:///{DB}")
    os.environ.setdefault("TEST_DATABASE_URL", f"sqlite:///{DB}")
    os.environ.setdefault("TESTING", "true")
    from app.ee import license as lic

    lic._get_license_key = lambda: "sandbox"
    lic._validate_license_key = lambda key: lic.LicenseInfo(
        licensed=True, tier="enterprise", org_name="llm-delegation-sandbox", features=FEATURES,
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


class Api:
    def __init__(self):
        import httpx
        self.c = httpx.Client(base_url=BASE, timeout=120)

    def ok(self, r, *codes):
        if r.status_code not in (codes or (200, 201, 204)):
            sys.exit(f"{r.request.method} {r.request.url} -> {r.status_code} {r.text}")
        return r.json() if r.content else None

    def login(self, email):
        r = self.c.post("/api/auth/jwt/login", data={"username": email, "password": PASSWORD})
        return self.ok(r)["access_token"]

    def headers(self, token, org_id=None):
        h = {"Authorization": f"Bearer {token}"}
        if org_id:
            h["X-Organization-Id"] = org_id
        return h


def _state():
    path = BACKEND / "db" / "llm_delegation_live.json"
    return json.loads(path.read_text()) if path.exists() else {}


def seed():
    api = Api()
    c, ok = api.c, api.ok
    admin_email = "admin@example.com"
    c.post("/api/auth/register", json={"name": "Ada Admin", "email": admin_email, "password": PASSWORD})
    h = api.headers(api.login(admin_email))
    orgs = ok(c.get("/api/organizations", headers=h))
    org_id = orgs[0]["id"] if orgs else ok(c.post("/api/organizations", json={"name": "Acme Analytics"}, headers=h))["id"]
    h["X-Organization-Id"] = org_id
    c.put("/api/organization/onboarding", json={"dismissed": True}, headers=h)

    providers = {p["name"]: p for p in ok(c.get("/api/llm/providers", headers=h))}
    for name, ptype in (("Anthropic", "anthropic"), ("OpenAI (Finance)", "openai"), ("Gemini (Marketing)", "google")):
        if name not in providers:
            providers[name] = ok(c.post("/api/llm/providers", json={
                "name": name, "provider_type": ptype, "credentials": {"api_key": "sk-sandbox"}, "models": [],
            }, headers=h))
    pid = {n: p["id"] for n, p in providers.items()}

    models = ok(c.get("/api/llm/models", params={"kind": "all", "manage": "true"}, headers=h))

    def mid(provider, name):
        return next(m["id"] for m in models if m["provider"]["id"] == pid[provider] and m["name"] == name)

    sonnet, haiku = mid("Anthropic", "Claude Sonnet 5.5"), mid("Anthropic", "Claude Haiku 5.5")
    ok(c.post(f"/api/llm/models/{sonnet}/set_default", params={"small": "false"}, headers=h))
    ok(c.post(f"/api/llm/models/{haiku}/set_default", params={"small": "true"}, headers=h))
    keep = {
        "Anthropic": {"Claude Sonnet 5.5", "Claude Haiku 5.5"},
        "OpenAI (Finance)": {"GPT-6 Sol", "GPT-6 Luna", "GPT-6 Luna Decisions", "GPT-6.1 Terra"},
        "Gemini (Marketing)": {"Gemini 4 Argon", "Gemini 3.6 Flash", "Gemini 3.5 Flash-Lite"},
    }
    switched_off = {"GPT-6.1 Terra", "Gemini 3.5 Flash-Lite"}
    by_pid = {v: k for k, v in pid.items()}
    for m in ok(c.get("/api/llm/models", params={"kind": "all", "manage": "true"}, headers=h)):
        pname = by_pid.get(m["provider"]["id"])
        want = m["name"] in keep.get(pname, set()) and m["name"] not in switched_off
        if m["is_enabled"] != want and not (m["is_default"] or m["is_small_default"]):
            ok(c.post(f"/api/llm/models/{m['id']}/toggle", params={"enabled": want}, headers=h))

    users = {}
    for key, name in (("alice", "Alice Finance-Lead"), ("fiona", "Fiona Finance"), ("bob", "Bob Marketing-Lead"),
                      ("mark", "Mark Marketing"), ("bianca", "Bianca Both"), ("nate", "Nate NoGroup")):
        email = f"{key}@example.com"
        c.post(f"/api/organizations/{org_id}/members", json={"organization_id": org_id, "email": email, "role": "member"}, headers=h)
        token = _invite_token(email)
        body = {"name": name, "email": email, "password": PASSWORD}
        if token:
            body["invite_token"] = token
        c.post("/api/auth/register", json=body)
        me = ok(c.get("/api/users/whoami", headers=api.headers(api.login(email))))
        users[key] = {"email": email, "id": me["id"], "name": name}

    groups = {g["name"]: g["id"] for g in ok(c.get(f"/api/organizations/{org_id}/groups", headers=h))}
    for g in ("Finance", "Marketing"):
        if g not in groups:
            groups[g] = ok(c.post(f"/api/organizations/{org_id}/groups", json={"name": g}, headers=h))["id"]
    for key, gnames in (("alice", ["Finance"]), ("fiona", ["Finance"]), ("bob", ["Marketing"]),
                        ("mark", ["Marketing"]), ("bianca", ["Finance", "Marketing"])):
        for g in gnames:
            c.post(f"/api/organizations/{org_id}/groups/{groups[g]}/members", json={"user_id": users[key]["id"]}, headers=h)
    for g, key in (("Finance", "alice"), ("Marketing", "bob")):
        ok(c.put(f"/api/organizations/{org_id}/groups/{groups[g]}/managers/{users[key]['id']}", headers=h))

    finance_models = [mid("OpenAI (Finance)", n) for n in ("GPT-6 Sol", "GPT-6 Luna", "GPT-6 Luna Decisions")]
    marketing_models = [mid("Gemini (Marketing)", n) for n in ("Gemini 4 Argon", "Gemini 3.6 Flash")]
    for g, ids in (("Finance", finance_models), ("Marketing", marketing_models)):
        for m in ids:
            ok(c.put(f"/api/llm/models/{m}/restricted", json={"is_restricted": True}, headers=h))
            c.post(f"/api/llm/models/{m}/access", json={"principal_type": "group", "principal_id": groups[g]}, headers=h)
    for provider, key in (("OpenAI (Finance)", "alice"), ("Gemini (Marketing)", "bob")):
        ok(c.put(f"/api/llm/providers/{pid[provider]}/managers/{users[key]['id']}", headers=h))

    for m, hint in ((haiku, "Quick lookups and follow-ups"), (sonnet, "Standard analysis and dashboards")):
        ok(c.post(f"/api/llm/models/{m}/routing_hint", json={"hint": hint}, headers=h))
    decision = mid("OpenAI (Finance)", "GPT-6 Luna Decisions")
    ok(c.put("/api/organization/settings", json={"config": {
        "model_routing": {"value": True},
        "model_routing_decision": {"value": {"method": "decision", "decision_model_id": decision}},
    }}, headers=h))

    state = {"org_id": org_id, "providers": pid, "groups": groups, "users": users, "models": {
        "sonnet": sonnet, "haiku": haiku,
        "sol": mid("OpenAI (Finance)", "GPT-6 Sol"), "luna": mid("OpenAI (Finance)", "GPT-6 Luna"),
        "terra": mid("OpenAI (Finance)", "GPT-6.1 Terra"), "decision": decision,
        "argon": mid("Gemini (Marketing)", "Gemini 4 Argon"), "flash": mid("Gemini (Marketing)", "Gemini 3.6 Flash"),
        "flashlite": mid("Gemini (Marketing)", "Gemini 3.5 Flash-Lite"),
    }}
    (BACKEND / "db" / "llm_delegation_live.json").write_text(json.dumps(state, indent=2))
    print(json.dumps({"org_id": org_id, "groups": groups, "providers": pid}, indent=2))


def check():
    api = Api()
    st = _state()
    out = {}
    for key, u in st["users"].items():
        h = api.headers(api.login(u["email"]), st["org_id"])
        ms = api.ok(api.c.get("/api/llm/models", params={"is_enabled": True}, headers=h))
        d = next((m for m in ms if m.get("is_inherited_default")), None)
        me = api.ok(api.c.get("/api/users/whoami", headers=h))
        org = next(o for o in me["organizations"] if o["id"] == st["org_id"])
        rp = org.get("resource_permissions") or {}
        out[u["email"]] = {
            "default": d and d["name"],
            "source": d and ((d.get("inherited_default_source") or "org") + (f":{d['inherited_default_group_name']}" if d.get("inherited_default_group_name") else "")),
            "chat_models": sorted(m["name"] for m in ms),
            "manages_providers": sorted(n for n, p in st["providers"].items() if f"llm_provider:{p}" in rp),
            "manages_groups": sorted(n for n, g in st["groups"].items() if f"group:{g}" in rp),
        }
    print(json.dumps(out, indent=2))


def attack():
    """Every row is (actor, method, path, body, expected status). Run as real
    sessions over HTTP; prints PASS/FAIL per row and a total."""
    api = Api()
    st = _state()
    o, P, G, M, U = st["org_id"], st["providers"], st["groups"], st["models"], st["users"]
    tok = {k: api.login(v["email"]) for k, v in U.items()}
    tok["admin"] = api.login("admin@example.com")
    A, B = P["OpenAI (Finance)"], P["Gemini (Marketing)"]
    rows = [
        # provider manager: own provider
        ("alice", "GET", "/api/llm/providers", None, 200, "alice lists providers (sees OpenAI (Finance) only)"),
        ("alice", "GET", "/api/llm/models?kind=all&manage=true", None, 200, "alice lists her provider's models"),
        ("alice", "POST", f"/api/llm/models/{M['sol']}/routing_hint", {"hint": "Finance deep analysis"}, 200, "alice sets a hint on GPT-6 Sol"),
        ("alice", "POST", f"/api/llm/models/{M['sol']}/set_temperature?temperature=0.2", None, 200, "alice sets GPT-6 Sol temperature"),
        ("alice", "PUT", f"/api/llm/providers/{A}", {"credentials": {"api_key": "sk-rotated"}}, 200, "alice rotates her provider's key"),
        # provider manager: other provider
        ("alice", "POST", f"/api/llm/models/{M['argon']}/routing_hint", {"hint": "mine now"}, 403, "alice hints Bob's Gemini 4 Argon"),
        ("alice", "POST", f"/api/llm/models/{M['flashlite']}/toggle?enabled=true", None, 403, "alice turns on Bob's model"),
        ("alice", "PUT", f"/api/llm/providers/{B}", {"credentials": {"api_key": "x"}}, 403, "alice edits Bob's provider"),
        ("alice", "POST", f"/api/llm/providers/{B}/toggle?enabled=false", None, 403, "alice turns off Bob's provider"),
        ("alice", "PUT", f"/api/llm/providers/{B}/managers/{U['alice']['id']}", None, 403, "alice makes herself Bob's provider manager"),
        # provider manager: org-wide
        ("alice", "POST", f"/api/llm/models/{M['sol']}/set_default?small=false", None, 403, "alice makes GPT-6 Sol the org default"),
        ("alice", "PUT", f"/api/llm/models/{M['sol']}/restricted", {"is_restricted": False}, 403, "alice opens GPT-6 Sol to everyone"),
        ("alice", "POST", f"/api/llm/models/{M['sol']}/access", {"principal_type": "user", "principal_id": U['nate']['id']}, 403, "alice grants nate GPT-6 Sol"),
        ("alice", "POST", f"/api/llm/models/{M['sol']}/pricing", {"input_cost_per_million_tokens_usd": 0.0}, 403, "alice zeroes GPT-6 Sol's price"),
        ("alice", "DELETE", f"/api/llm/providers/{A}", None, 403, "alice deletes her provider"),
        ("alice", "POST", "/api/llm/providers", {"name": "Rogue", "provider_type": "openai", "credentials": {"api_key": "k"}, "models": []}, 403, "alice creates a provider"),
        ("alice", "POST", "/api/llm/fallback_order", {"model_ids": [M['sol']]}, 403, "alice sets the fallback order"),
        ("alice", "PUT", "/api/organization/settings", {"config": {"model_routing": {"value": False}}}, 403, "alice turns off the Auto router"),
        ("alice", "POST", f"/api/organizations/{o}/resource-grants", {"resource_type": "llm_provider", "resource_id": B, "principal_type": "user", "principal_id": U['alice']['id'], "permissions": ["manage"]}, 422, "alice mints a provider grant (refused at validation)"),
        ("admin", "POST", f"/api/organizations/{o}/resource-grants", {"resource_type": "llm_provider", "resource_id": B, "principal_type": "group", "principal_id": G['Finance'], "permissions": ["manage"]}, 422, "admin mints a provider grant via generic route"),
        # group manager
        ("alice", "GET", "/api/llm/group-defaults", None, 200, "alice lists group defaults (Finance only)"),
        ("alice", "PUT", f"/api/llm/group-defaults/{G['Finance']}", {"default_llm_model_id": M['sol'], "small_default_llm_model_id": M['luna']}, 200, "alice sets Finance → GPT-6 Sol / GPT-6 Luna"),
        ("alice", "PUT", f"/api/llm/group-defaults/{G['Finance']}", {"default_llm_model_id": M['argon']}, 403, "alice gives Finance Gemini 4 Argon"),
        ("alice", "PUT", f"/api/llm/group-defaults/{G['Finance']}", {"default_llm_priority": 1}, 403, "alice bumps Finance's priority"),
        ("alice", "PUT", f"/api/llm/group-defaults/{G['Marketing']}", {"default_llm_model_id": M['sonnet']}, 403, "alice edits Marketing's defaults"),
        ("bob", "PUT", f"/api/llm/group-defaults/{G['Marketing']}", {"default_llm_model_id": M['argon'], "small_default_llm_model_id": M['flash']}, 200, "bob sets Marketing → Gemini 4 Argon / 3.6 Flash"),
        ("bob", "GET", f"/api/llm/group-defaults/{G['Finance']}/choices", None, 403, "bob reads Finance's choices"),
        # plain members
        ("fiona", "GET", "/api/llm/group-defaults", None, 403, "fiona lists group defaults"),
        ("fiona", "GET", "/api/llm/models?manage=true", None, 403, "fiona lists models to manage"),
        ("fiona", "POST", f"/api/llm/models/{M['sol']}/routing_hint", {"hint": "x"}, 403, "fiona sets a hint"),
        ("nate", "GET", "/api/llm/providers", None, 403, "nate lists providers"),
        # restrict on enable
        ("alice", "POST", f"/api/llm/models/{M['terra']}/toggle?enabled=true", None, 200, "alice turns on GPT-6.1 Terra (starts restricted)"),
    ]
    passed = 0
    for actor, method, path, body, want, label in rows:
        h = api.headers(tok[actor], o)
        r = api.c.request(method, path, headers=h, json=body)
        good = r.status_code == want
        passed += good
        print(f"{'PASS' if good else 'FAIL'}  {label:<62} {r.status_code} (want {want})")
    h = api.headers(tok["admin"], o)
    terra = next(m for m in api.ok(api.c.get("/api/llm/models", params={"kind": "all", "manage": "true"}, headers=h)) if m["id"] == M["terra"])
    good = terra["is_enabled"] and terra["is_restricted"]
    passed += good
    print(f"{'PASS' if good else 'FAIL'}  {'GPT-6.1 Terra is on and restricted':<62} enabled={terra['is_enabled']} restricted={terra['is_restricted']}")
    print(f"{passed}/{len(rows) + 1} as expected")


if __name__ == "__main__":
    {"serve": serve, "seed": seed, "attack": attack, "check": check}[sys.argv[1] if len(sys.argv) > 1 else "serve"]()
