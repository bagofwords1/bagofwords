#!/usr/bin/env python3
"""Seed the per-provider PII exemption loop through the real API.

Registers two OpenAI-compatible providers backed by pii_capture_llm.py —
"In-VPC Llama" (trusted) and "Public API" — makes the public one the org
default, and turns PII protection on (replace mode, built-in rules). It does
NOT exempt anything: the loop exempts the trusted provider from the UI.

    cd backend && uv run python ../tools/agent/pii_provider_exemption_setup.py

Prints the provider ids as JSON. Env/flags: --base-url (default
http://localhost:8000), --mock-url (default http://127.0.0.1:9199),
BOW_EMAIL / BOW_PASSWORD (default admin@example.com / Password123!).
"""
import argparse
import json
import os

import httpx

p = argparse.ArgumentParser()
p.add_argument("--base-url", default="http://localhost:8000")
p.add_argument("--mock-url", default="http://127.0.0.1:9199")
a = p.parse_args()

c = httpx.Client(base_url=a.base_url, timeout=60, trust_env=False)
tok = c.post("/api/auth/jwt/login", data={
    "username": os.environ.get("BOW_EMAIL", "admin@example.com"),
    "password": os.environ.get("BOW_PASSWORD", "Password123!"),
}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}

existing = {pr["name"]: pr for pr in c.get("/api/llm/providers", headers=H).json()}
ids = {}
for key, name, model_id in (("trusted", "In-VPC Llama", "llama-3.3-70b"), ("public", "Public API", "gpt-public")):
    if name in existing:
        ids[key] = existing[name]["id"]
        continue
    r = c.post("/api/llm/providers", json={
        "name": name, "provider_type": "custom",
        "credentials": {"base_url": f"{a.mock_url}/{key}/v1", "api_key": "sk-mock"},
        "models": [{"model_id": model_id, "name": model_id, "is_custom": True, "is_enabled": True}],
    }, headers=H)
    r.raise_for_status()
    ids[key] = r.json()["id"]

public_models = [m for m in c.get("/api/llm/models", headers=H).json() if m.get("model_id") == "gpt-public"]
for m in public_models:
    c.post(f"/api/llm/models/{m['id']}/set_default", headers=H).raise_for_status()
    c.post(f"/api/llm/models/{m['id']}/set_default", params={"small": True}, headers=H)

r = c.put("/api/organization/settings", json={"config": {"pii_protection": {
    "enabled": True, "mode": "replace", "exempt_provider_ids": [],
}}}, headers=H)
r.raise_for_status()
print(json.dumps({"org_id": org, **ids}))
