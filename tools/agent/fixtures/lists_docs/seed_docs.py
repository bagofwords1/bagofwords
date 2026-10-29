"""Seed the Lists docs workspace: a 'Contract Desk' agent over the fictional
contracts plus two lists. Run against a backend on a fresh DB after
seed_org.py + setup_openai_llm.py (see README.md in this folder).

    backend/.venv/bin/python tools/agent/fixtures/lists_docs/seed_docs.py [contracts_dir]
"""
import json
import sys

import httpx

BASE = "http://localhost:8000"
EMAIL, PASSWORD = "maya@northwind.example", "Password123!"
ROOT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/bow-docs-contracts"

c = httpx.Client(base_url=BASE, timeout=120)
tok = c.post("/api/auth/jwt/login", data={"username": EMAIL, "password": PASSWORD}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
c.headers.update({"Authorization": f"Bearer {tok}", "X-Organization-Id": org})

agents = {d["name"]: d for d in c.get("/api/data_sources").json()}
agent = agents.get("Contract Desk")
if not agent:
    r = c.post("/api/data_sources", json={
        "name": "Contract Desk", "type": "network_dir",
        "description": "Customer contracts, order forms and SOWs for the commercial team.",
        "config": {"root_path": ROOT}, "credentials": {"auth_type": "none"}, "auth_policy": "system_only",
        "generate_summary": False, "generate_conversation_starters": False, "generate_ai_rules": False,
    })
    r.raise_for_status()
    agent = r.json()
aid = agent["id"]

LISTS = [
    {"name": "Contracts", "description": "One row per customer, from the signed agreements and their amendments.",
     "key_field": "counterparty", "require_evidence": True, "fields": [
        {"name": "counterparty", "type": "string", "description": "Customer legal name", "required": True},
        {"name": "contract_type", "type": "enum", "enum": ["MSA", "Order Form", "SOW"], "description": "Document type"},
        {"name": "annual_value", "type": "number", "description": "Current annual fee; annualize monthly fees and say so in the note"},
        {"name": "currency", "type": "enum", "enum": ["USD", "EUR", "GBP"]},
        {"name": "renewal_date", "type": "date", "description": "End of the current term"},
        {"name": "auto_renew", "type": "boolean", "description": "Renews automatically?"},
        {"name": "notice_days", "type": "integer", "description": "Notice period to stop renewal or terminate", "unit": "days"},
        {"name": "governing_law", "type": "string"},
    ]},
    {"name": "Obligations", "description": "One row per concrete obligation a party has (payments, notices, reports, deadlines).",
     "require_evidence": True, "fields": [
        {"name": "counterparty", "type": "string", "required": True},
        {"name": "party", "type": "enum", "enum": ["Northwind", "Customer", "Both"], "description": "Who owes it"},
        {"name": "obligation", "type": "string", "description": "The obligation, in one short sentence", "required": True},
        {"name": "due", "type": "string", "description": "When it is due, as written"},
    ]},
]
existing = {l["name"] for l in c.get(f"/api/data_sources/{aid}/lists").json()}
for spec in LISTS:
    if spec["name"] in existing:
        continue
    key = spec.pop("key_field", None)
    body = dict(spec)
    if key:
        body["key_field"] = key
    r = c.post(f"/api/data_sources/{aid}/lists", json=body)
    print(spec["name"], r.status_code, r.text[:200] if r.status_code >= 300 else "")
print(json.dumps({"agent_id": aid, "lists": [l["name"] for l in c.get(f"/api/data_sources/{aid}/lists").json()]}))
