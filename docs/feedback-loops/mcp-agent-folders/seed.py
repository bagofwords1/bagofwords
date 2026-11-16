"""Seed a running BOW backend for the MCP folder-scope feedback loop.

Org "Acme Payments":
  Payments/            (folder)
    Disputes/          disputes_ledger agent   (sqlite: disputes, chargeback_notices)
    Authorization/     auth_events agent       (sqlite: authorizations)
  Refunds/             refunds agent           (sqlite: refunds)
  Marketing/           campaigns agent         (sqlite: campaigns)
  Executive/  (private) exec_kpis + exec_comp  (sqlite: kpis / comp)

Personas (each gets an MCP API key):
  admin        org admin
  dana         query-only analyst: Viewer on Payments, Refunds, Marketing
  cory         curator: custom role view+manage_instructions on Disputes & Authorization,
               manage_instructions on the refunds agent, Viewer on Refunds/Marketing
  mo           manager of one target: Manager on Disputes, Viewer elsewhere
  alex         manager of all targets: Manager on Payments + Refunds, Viewer on Marketing
  gus          guest with a direct grant on exec_kpis only

Writes seed.json next to this file (gitignored: it holds tokens and API keys).
Run against a fresh sandbox DB:  cd backend && uv run python ../docs/feedback-loops/mcp-agent-folders/seed.py
"""
import json
import pathlib
import random
import sqlite3
import sys

import httpx

BASE = "http://localhost:8000"
HERE = pathlib.Path(__file__).resolve().parent
APP_DB = pathlib.Path("/home/user/bagofwords/backend/db/app.db")
DATA = HERE / "data"
PW = "Password123!"


def make_data():
    DATA.mkdir(exist_ok=True)
    rnd = random.Random(7)
    specs = {
        "disputes.db": [
            ("CREATE TABLE disputes (id INTEGER PRIMARY KEY, opened_at TEXT, merchant_id INTEGER, amount_cents INTEGER, reason TEXT, status TEXT)",
             lambda i: (i, f"2026-{rnd.randint(1,10):02d}-{rnd.randint(1,28):02d}", rnd.randint(1, 40), rnd.randint(500, 90000),
                        rnd.choice(["fraud", "not_received", "duplicate", "credit_not_processed"]), rnd.choice(["open", "won", "lost"]))),
            ("CREATE TABLE chargeback_notices (id INTEGER PRIMARY KEY, dispute_id INTEGER, notice_date TEXT, notice_type TEXT)",
             lambda i: (i, rnd.randint(1, 300), f"2026-{rnd.randint(1,10):02d}-{rnd.randint(1,28):02d}", rnd.choice(["first", "second", "pre_arb"]))),
        ],
        "auth.db": [
            ("CREATE TABLE authorizations (id INTEGER PRIMARY KEY, created_at TEXT, merchant_id INTEGER, amount_cents INTEGER, outcome TEXT, decline_code TEXT)",
             lambda i: (i, f"2026-{rnd.randint(1,10):02d}-{rnd.randint(1,28):02d}", rnd.randint(1, 40), rnd.randint(100, 50000),
                        rnd.choice(["approved"] * 8 + ["declined"] * 2), rnd.choice([None, "05", "51", "54"]))),
        ],
        "refunds.db": [
            ("CREATE TABLE refunds (id INTEGER PRIMARY KEY, refunded_at TEXT, merchant_id INTEGER, amount_cents INTEGER, kind TEXT)",
             lambda i: (i, f"2026-{rnd.randint(1,10):02d}-{rnd.randint(1,28):02d}", rnd.randint(1, 40), rnd.randint(100, 20000), rnd.choice(["full", "partial"]))),
        ],
        "marketing.db": [
            ("CREATE TABLE campaigns (id INTEGER PRIMARY KEY, name TEXT, channel TEXT, spend_cents INTEGER, signups INTEGER)",
             lambda i: (i, f"Campaign {i}", rnd.choice(["search", "social", "email"]), rnd.randint(10000, 900000), rnd.randint(0, 400))),
        ],
        "exec.db": [
            ("CREATE TABLE kpis (month TEXT, gross_volume_cents INTEGER, net_revenue_cents INTEGER)",
             lambda i: (f"2026-{(i % 10) + 1:02d}", rnd.randint(10**9, 10**10), rnd.randint(10**7, 10**8))),
        ],
        "comp.db": [
            ("CREATE TABLE comp (employee TEXT, salary_cents INTEGER)",
             lambda i: (f"emp{i}", rnd.randint(5_000_000, 30_000_000))),
        ],
    }
    for fname, tables in specs.items():
        path = DATA / fname
        if path.exists():
            path.unlink()
        con = sqlite3.connect(path)
        for ddl, row in tables:
            con.execute(ddl)
            cols = ddl.count(",") + 1
            con.executemany(f"INSERT INTO {ddl.split()[2]} VALUES ({','.join('?' * cols)})", [row(i) for i in range(1, 301)])
        con.commit()
        con.close()


def main():
    make_data()
    c = httpx.Client(base_url=BASE, timeout=120)

    def register(name, email, token=None):
        body = {"name": name, "email": email, "password": PW}
        if token:
            body["invite_token"] = token
        r = c.post("/api/auth/register", json=body)
        assert r.status_code in (201, 400), r.text

    def login(email):
        r = c.post("/api/auth/jwt/login", data={"username": email, "password": PW})
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    register("Ada Admin", "admin@acme.example.com")
    admin_tok = login("admin@acme.example.com")
    org = c.get("/api/organizations", headers={"Authorization": f"Bearer {admin_tok}"}).json()[0]["id"]
    H = {"Authorization": f"Bearer {admin_tok}", "X-Organization-Id": org}

    def api(method, path, body=None, headers=H):
        r = c.request(method, "/api/" + path, json=body, headers=headers)
        if not r.is_success:
            sys.exit(f"{method} {path} -> {r.status_code} {r.text}")
        return r.json()

    api("PUT", "organization/settings", {"config": {"mcp_enabled": {"value": True}}})
    # Skip onboarding so the web UI is usable for spot checks.
    try:
        api("PUT", "organization/onboarding", {"dismissed": True})
    except SystemExit:
        pass

    def folder(name, parent=None):
        return api("POST", "folders", {"name": name, **({"parent_id": parent} if parent else {})})["id"]

    F = {}
    F["payments"] = folder("Payments")
    F["disputes"] = folder("Disputes", F["payments"])
    F["authorization"] = folder("Authorization", F["payments"])
    F["refunds"] = folder("Refunds")
    F["marketing"] = folder("Marketing")
    F["executive"] = folder("Executive")

    def agent(name, db, home):
        ds = api("POST", "data_sources", {"name": name, "type": "sqlite", "config": {"database": str(DATA / db)},
                                          "credentials": {}, "auth_policy": "system_only", "generate_summary": False,
                                          "generate_conversation_starters": False, "generate_ai_rules": False})
        api("PUT", f"data_sources/{ds['id']}", {"is_public": False})
        api("PUT", f"folders/items/agent/{ds['id']}/placement", {"parent_id": home})
        api("GET", f"data_sources/{ds['id']}/refresh_schema")
        api("POST", f"data_sources/{ds['id']}/bulk_update_tables", {"action": "activate"})
        return ds["id"]

    A = {
        "disputes": agent("disputes_ledger", "disputes.db", F["disputes"]),
        "auth": agent("auth_events", "auth.db", F["authorization"]),
        "refunds": agent("refunds", "refunds.db", F["refunds"]),
        "campaigns": agent("campaigns", "marketing.db", F["marketing"]),
        "exec_kpis": agent("exec_kpis", "exec.db", F["executive"]),
        "exec_comp": agent("exec_comp", "comp.db", F["executive"]),
    }

    users = {"admin": {"email": "admin@acme.example.com", "token": admin_tok}}
    for key, name in [("dana", "Dana Query"), ("cory", "Cory Curator"), ("mo", "Mo OneManager"),
                      ("alex", "Alex AllManager"), ("gus", "Gus Guest")]:
        email = f"{key}@acme.example.com"
        r = c.post(f"/api/organizations/{org}/members", json={"organization_id": org, "email": email, "role": "member"}, headers=H)
        assert r.status_code in (200, 400, 409), r.text
        con = sqlite3.connect(APP_DB)
        row = con.execute("SELECT invite_token FROM memberships WHERE email=? AND user_id IS NULL ORDER BY created_at DESC LIMIT 1",
                          (email,)).fetchone()
        con.close()
        register(name, email, row[0] if row else None)
        tok = login(email)
        uid = c.get("/api/users/whoami", headers={"Authorization": f"Bearer {tok}"}).json()["id"]
        users[key] = {"email": email, "token": tok, "user_id": uid}

    def share(f, who, role):
        api("PUT", f"folders/{F[f]}/share", {"principal_type": "user", "principal_id": users[who]["user_id"], "role": role})

    for f in ("payments", "refunds", "marketing"):
        share(f, "dana", "Viewer")
        share(f, "alex", "Manager" if f != "marketing" else "Viewer")
    share("disputes", "mo", "Manager")
    for f in ("authorization", "refunds", "marketing"):
        share(f, "mo", "Viewer")
    for f in ("refunds", "marketing"):
        share(f, "cory", "Viewer")
    role = api("POST", f"organizations/{org}/roles", {"name": "Payments curator", "resource_grants": [
        {"resource_type": "folder", "resource_id": F["disputes"], "permissions": ["view", "manage_instructions"]},
        {"resource_type": "folder", "resource_id": F["authorization"], "permissions": ["view", "manage_instructions"]},
        {"resource_type": "data_source", "resource_id": A["refunds"], "permissions": ["manage_instructions"]},
    ]})
    api("POST", f"organizations/{org}/role-assignments", {"role_id": role["id"], "principal_type": "user",
                                                       "principal_id": users["cory"]["user_id"]})
    api("POST", f"data_sources/{A['exec_kpis']}/members", {"principal_type": "user", "principal_id": users["gus"]["user_id"]})

    for key, u in users.items():
        hdr = {"Authorization": f"Bearer {u['token']}", "X-Organization-Id": org}
        u["key"] = api("POST", "api_keys", {"name": "mcp loop"}, headers=hdr)["key"]

    out = {"org_id": org, "folders": F, "agents": A, "users": users}
    (HERE / "seed.json").write_text(json.dumps(out, indent=2))
    print(json.dumps({"org": org, "folders": F, "agents": A, "users": list(users)}, indent=2))


if __name__ == "__main__":
    main()
