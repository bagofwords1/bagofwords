#!/usr/bin/env python3
"""Set up the per-agent PII policies loop (docs/feedback-loops/pii-agent-policies.md).

Creates, through the real API (idempotent):

* "Mock LLM" — an OpenAI-compatible provider at the capture mock
  (tools/agent/pii_capture_llm.py, :9199/public/v1), the org default model;
* "Anthropic" — only when ANTHROPIC_KEY_LOOP is set (never printed);
* two agents on fresh SQLite files with planted PII:
    HR    (employees: name, email, phone)
    Sales (accounts: company, contact_email)
* PII protection on, with policies
    default  — every built-in rule off (so only agent policies redact)
    hr       — "HR (emails + phones)": email + phone replaced
    sales    — "Sales (emails)": email replaced
  and NO agent assignments (the loop assigns them in the UI / per case).

Prints a JSON summary: agent ids, provider ids, model ids.

Run (from backend/): uv run python ../tools/agent/pii_agent_policies_setup.py
Env: BOW_EMAIL / BOW_PASSWORD (default admin@example.com / Password123!),
     BOW_BASE_URL (default http://localhost:8000), ANTHROPIC_KEY_LOOP (optional),
     PII_AGENT_DATA_DIR (default /tmp/bow-agent/pii-agents).
"""
import json
import os
import pathlib
import sqlite3

import httpx

BASE = os.environ.get("BOW_BASE_URL", "http://localhost:8000")
DATA = pathlib.Path(os.environ.get("PII_AGENT_DATA_DIR", "/tmp/bow-agent/pii-agents"))
OFF = {rid: {"enabled": False} for rid in ("email", "credit_card", "us_ssn", "phone", "ipv4", "iban", "aws_access_key")}

c = httpx.Client(base_url=BASE, timeout=120)
tok = c.post("/api/auth/jwt/login", data={
    "username": os.environ.get("BOW_EMAIL", "admin@example.com"),
    "password": os.environ.get("BOW_PASSWORD", "Password123!"),
}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}


def ok(r):
    if r.status_code >= 400:
        raise SystemExit(f"{r.request.method} {r.request.url} -> {r.status_code} {r.text[:500]}")
    return r.json()


# --- providers -----------------------------------------------------------------
providers = {p["name"]: p for p in ok(c.get("/api/llm/providers", headers=H))}
if "Mock LLM" not in providers:
    ok(c.post("/api/llm/providers", json={
        "name": "Mock LLM", "provider_type": "custom",
        "credentials": {"base_url": "http://127.0.0.1:9199/public/v1", "api_key": "sk-mock"},
        "models": [{"model_id": "mock-public", "name": "Mock public", "is_custom": True}],
    }, headers=H))
anthropic_key = os.environ.get("ANTHROPIC_KEY_LOOP")
if anthropic_key and "Anthropic" not in providers:
    ok(c.post("/api/llm/providers", json={
        "name": "Anthropic", "provider_type": "anthropic",
        "credentials": {"api_key": anthropic_key},
        "models": [{"model_id": "claude-haiku-4-5", "name": "Claude Haiku 4.5", "is_custom": False}],
    }, headers=H))
providers = {p["name"]: p for p in ok(c.get("/api/llm/providers", headers=H))}
models = ok(c.get("/api/llm/models", headers=H))
by_model = {m["model_id"]: m for m in models}
mock = by_model.get("mock-public")
if mock:
    c.post(f"/api/llm/models/{mock['id']}/set_default", headers=H)
    c.post(f"/api/llm/models/{mock['id']}/set_default", params={"small": True}, headers=H)

# --- agents --------------------------------------------------------------------
DATA.mkdir(parents=True, exist_ok=True)
SEED = {
    "HR": ("hr.db", "CREATE TABLE employees (name TEXT, email TEXT, phone TEXT)",
           "INSERT INTO employees VALUES (?, ?, ?)",
           [("Dana Levi", "dana.levi@acme-hr.example", "415-555-0101"),
            ("Omar Haddad", "omar.haddad@acme-hr.example", "415-555-0102"),
            ("Rina Cohen", "rina.cohen@acme-hr.example", "415-555-0103")]),
    "Sales": ("sales.db", "CREATE TABLE accounts (company TEXT, contact_email TEXT, region TEXT)",
              "INSERT INTO accounts VALUES (?, ?, ?)",
              [("Globex", "buyer@globex.example", "EMEA"),
               ("Initech", "ap@initech.example", "NA"),
               ("Umbrella", "procurement@umbrella.example", "APAC")]),
}
existing = {d["name"]: d for d in ok(c.get("/api/data_sources", params={"show_all": True}, headers=H))}
agents = {}
for name, (fname, ddl, ins, rows) in SEED.items():
    path = DATA / fname
    if not path.exists():
        with sqlite3.connect(path) as conn:
            conn.execute(ddl)
            conn.executemany(ins, rows)
    if name in existing:
        agents[name] = existing[name]["id"]
        continue
    ds = ok(c.post("/api/data_sources", json={
        "name": name, "type": "sqlite", "config": {"database": str(path)}, "credentials": {},
        "auth_policy": "system_only", "generate_summary": False,
        "generate_conversation_starters": False, "generate_ai_rules": False,
    }, headers=H))
    agents[name] = ds["id"]

# Make each agent's table active so the model can query it (also on reruns).
TABLES = {"HR": "employees", "Sales": "accounts"}
for name, ds_id in agents.items():
    r = ok(c.put(f"/api/data_sources/{ds_id}/update_tables_status",
                 json={"activate": [TABLES[name]], "deactivate": []}, headers=H))
    if not r.get("total_selected"):
        raise SystemExit(f"{name}: table {TABLES[name]} could not be activated: {r}")

# --- PII config --------------------------------------------------------------
ok(c.put("/api/organization/settings", json={"config": {"pii_protection": {
    "enabled": True,
    "policies": {
        "default": {"name": "Default", "mode": "replace", "builtin_overrides": OFF},
        "hr": {"name": "HR (emails + phones)", "mode": "replace",
               "builtin_overrides": {**OFF, "email": {"enabled": True}, "phone": {"enabled": True}}},
        "sales": {"name": "Sales (emails)", "mode": "replace",
                  "builtin_overrides": {**OFF, "email": {"enabled": True}}},
    },
    "provider_policies": {},
}}}, headers=H))

print(json.dumps({
    "org": org,
    "agents": agents,
    "providers": {k: v["id"] for k, v in providers.items()},
    "models": {k: v["id"] for k, v in by_model.items()},
}, indent=2))
