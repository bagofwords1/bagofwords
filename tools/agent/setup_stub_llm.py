#!/usr/bin/env python3
"""Register an OpenAI-compatible stub LLM (e.g. slow_stub_llm.py) as the org's
default model, through the real API.

    cd backend && uv run python ../tools/agent/setup_stub_llm.py \
        --base-url http://127.0.0.1:3000 --stub-url http://127.0.0.1:9099/v1
"""
import argparse

import httpx

p = argparse.ArgumentParser()
p.add_argument("--base-url", default="http://localhost:8000")
p.add_argument("--stub-url", default="http://127.0.0.1:9099/v1")
p.add_argument("--email", default="admin@example.com")
p.add_argument("--password", default="Password123!")
p.add_argument("--model-id", default="stub")
a = p.parse_args()

c = httpx.Client(base_url=a.base_url, timeout=60, trust_env=False)
tok = c.post("/api/auth/jwt/login", data={"username": a.email, "password": a.password}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}
r = c.post("/api/llm/providers", json={
    "name": "Stub", "provider_type": "custom",
    "credentials": {"base_url": a.stub_url, "api_key": "sk-stub"},
    "models": [{"model_id": a.model_id, "name": "Stub", "is_custom": True, "is_enabled": True}],
}, headers=H)
r.raise_for_status()
models = [m for m in c.get("/api/llm/models", headers=H).json() if m.get("model_id") == a.model_id]
for m in models:
    c.post(f"/api/llm/models/{m['id']}/set_default", headers=H).raise_for_status()
    c.post(f"/api/llm/models/{m['id']}/set_default", params={"small": True}, headers=H)
print("stub provider", r.json()["id"], "models", [m["id"] for m in models])
