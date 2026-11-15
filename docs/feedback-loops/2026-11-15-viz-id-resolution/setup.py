#!/usr/bin/env python3
"""Loop B setup: point the seeded org at a model.

Run after tools/agent/boot_stack.sh and tools/agent/seed_org.py --demo:

    cd backend && uv run python ../docs/feedback-loops/2026-11-15-viz-id-resolution/setup.py

By default it registers the scripted stub (stub_llm.py, STUB_BASE_URL) as the
default and small-default model. With BOW_LIVE_MODEL set it registers that
Anthropic model instead (needs ANTHROPIC_KEY with credit).

Prints JSON with the login and organization the browser driver needs.
"""
import json
import os
import sys

import httpx

BASE = os.environ.get("BOW_BASE_URL", "http://localhost:8000")
EMAIL = os.environ.get("BOW_EMAIL", "admin@example.com")
PASSWORD = os.environ.get("BOW_PASSWORD", "Password123!")
LIVE_MODEL = os.environ.get("BOW_LIVE_MODEL")
STUB_BASE_URL = os.environ.get("STUB_BASE_URL", "http://127.0.0.1:9099/v1")
MODEL = LIVE_MODEL or "stub"

c = httpx.Client(base_url=BASE, timeout=120)


def ok(resp):
    if resp.status_code >= 400:
        sys.exit(f"{resp.request.method} {resp.request.url} -> {resp.status_code} {resp.text[:400]}")
    return resp.json()


token = ok(c.post("/api/auth/jwt/login", data={"username": EMAIL, "password": PASSWORD}))["access_token"]
org = ok(c.get("/api/users/whoami", headers={"Authorization": f"Bearer {token}"}))["organizations"][0]["id"]
H = {"Authorization": f"Bearer {token}", "X-Organization-Id": org}

providers = ok(c.get("/api/llm/providers", headers=H))
# By model, not by provider type: an org can hold several custom providers.
provider = next((p for p in providers if any(m["model_id"] == MODEL for m in p.get("models") or [])), None)
if provider is None:
    if LIVE_MODEL:
        key = os.environ.get("ANTHROPIC_KEY") or sys.exit("ANTHROPIC_KEY not set")
        payload = {"name": "anthropic-live", "provider_type": "anthropic", "credentials": {"api_key": key},
                   "models": [{"model_id": MODEL, "name": MODEL, "is_custom": False, "is_default": True}]}
    else:
        payload = {"name": "loop-stub", "provider_type": "custom",
                   "credentials": {"base_url": STUB_BASE_URL, "api_key": "stub"},
                   "models": [{"model_id": MODEL, "name": MODEL, "is_custom": True}]}
    provider = ok(c.post("/api/llm/providers", headers=H, json=payload))
model = next(m for m in provider["models"] if m["model_id"] == MODEL)
for small in ("false", "true"):
    ok(c.post(f"/api/llm/models/{model['id']}/set_default?small={small}", headers=H))

sources = ok(c.get("/api/data_sources", headers=H))
print(json.dumps({
    "email": EMAIL, "password": PASSWORD, "org_id": org, "token": token, "model": MODEL,
    "data_sources": [{"id": s["id"], "name": s["name"]} for s in sources],
}, indent=2))
