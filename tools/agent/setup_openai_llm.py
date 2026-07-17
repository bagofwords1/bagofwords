#!/usr/bin/env python3
"""Configure an OpenAI LLM in a running BOW org via the API.

Reads the key from OPENAI_API_KEY. Idempotent-ish: reuses the openai provider
if present, then registers the chosen model as default + small default.

    cd backend && OPENAI_API_KEY=sk-... uv run python ../tools/agent/setup_openai_llm.py
"""
import os
import sys
import httpx

BASE = os.environ.get("BOW_BASE_URL", "http://localhost:8000")
EMAIL = os.environ.get("BOW_ADMIN_EMAIL", "admin@example.com")
PASSWORD = os.environ.get("BOW_ADMIN_PASSWORD", "Password123!")
MODEL_ID = os.environ.get("OPENAI_MODEL_ID", "gpt-5.6-luna")
MODEL_NAME = os.environ.get("OPENAI_MODEL_NAME", "GPT-5.6 Luna")

key = os.environ.get("OPENAI_API_KEY")
if not key:
    sys.exit("OPENAI_API_KEY is not set — cannot configure the LLM.")

c = httpx.Client(base_url=BASE, timeout=60)
tok = c.post("/api/auth/jwt/login", data={"username": EMAIL, "password": PASSWORD}).json()["access_token"]
orgs = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()
org = orgs[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}

# 1. Provider: reuse an existing openai provider, else create one.
providers = c.get("/api/llm/providers", headers=H).json()
prov = next((p for p in providers if p.get("provider_type") == "openai"), None)
if prov:
    pid = prov["id"]
    c.put(f"/api/llm/providers/{pid}", json={"credentials": {"api_key": key}, "is_enabled": True}, headers=H)
    print("reusing openai provider", pid)
else:
    r = c.post("/api/llm/providers", json={
        "name": "OpenAI", "provider_type": "openai",
        "credentials": {"api_key": key},
    }, headers=H)
    if r.status_code not in (200, 201):
        sys.exit(f"create provider failed: {r.status_code} {r.text}")
    pid = r.json()["id"]
    print("created openai provider", pid)

# 2. Test the provider connection (validates the key).
t = c.post("/api/llm/test_connection", json={
    "name": "OpenAI", "provider_type": "openai",
    "provider_id": pid, "credentials": {"api_key": key},
}, headers=H)
print("provider test_connection:", t.status_code, t.text[:200])

# 3. Model: register/enable the chosen model as default + small default.
models = c.get("/api/llm/models", headers=H).json()
existing = next((m for m in models if m.get("model_id") == MODEL_ID), None)
if existing:
    c.patch(f"/api/llm/models/{existing['id']}",
            json={"is_default": True, "is_small_default": True, "is_enabled": True}, headers=H)
    print("reused + defaulted model", existing["id"])
else:
    r = c.post("/api/llm/models", json={
        "provider_id": pid, "name": MODEL_NAME, "model_id": MODEL_ID,
        "is_default": True, "is_small_default": True,
        "context_window_tokens": 1050000, "max_output_tokens": 128000,
    }, headers=H)
    if r.status_code not in (200, 201):
        sys.exit(f"create model failed: {r.status_code} {r.text}")
    print("created default model", r.json().get("id"))

print("OPENAI LLM CONFIGURED")
