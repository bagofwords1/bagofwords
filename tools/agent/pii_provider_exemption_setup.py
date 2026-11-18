#!/usr/bin/env python3
"""Seed the PII policy loop through the real API.

Providers (OpenAI-compatible, backed by pii_capture_llm.py):
  "In-VPC Llama" (…/trusted/v1)  → assigned Exempt by the loop (not here)
  "Public API"   (…/public/v1)   → Default policy; its model is the org default
  "Strict API"   (…/strict/v1)   → assigned the "Strict" policy (block mode)

With ANTHROPIC_KEY_LOOP set, also an Anthropic provider with Claude Haiku 5.5
(a real model, on Default) for the live leg. The key goes to the BoW API only;
it is never printed or written to disk by this script.

PII protection is turned on with a Default policy (replace) and a Strict
policy (block); an org instruction containing an email address is added so
the loop can show instructions reach the model unredacted.

    cd backend && uv run python ../tools/agent/pii_provider_exemption_setup.py

Prints the ids as JSON. Flags: --base-url (http://localhost:8000),
--mock-url (http://127.0.0.1:9199), --tokens (the Default policy uses the
"hide from the model" action for emails and, when a detector is available,
names), --on-detector-error block|regex_only. Env: BOW_EMAIL / BOW_PASSWORD
(default admin@example.com / Password123!), ANTHROPIC_KEY_LOOP (optional).
"""
import argparse
import json
import os

import httpx

p = argparse.ArgumentParser()
p.add_argument("--base-url", default="http://localhost:8000")
p.add_argument("--mock-url", default="http://127.0.0.1:9199")
p.add_argument("--tokens", action="store_true",
               help="Default policy hides emails (and names, with a detector) from the model only")
p.add_argument("--on-detector-error", default="block", choices=["block", "regex_only"])
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
for key, name, model_id in (
    ("trusted", "In-VPC Llama", "llama-3.3-70b"),
    ("public", "Public API", "gpt-public"),
    ("strict", "Strict API", "gpt-strict"),
):
    if name in existing:
        ids[key] = existing[name]["id"]
        # Keep an existing provider pointed at the mock in use.
        c.put(f"/api/llm/providers/{ids[key]}", json={"credentials": {
            "base_url": f"{a.mock_url}/{key}/v1", "api_key": "sk-mock"}}, headers=H).raise_for_status()
        continue
    r = c.post("/api/llm/providers", json={
        "name": name, "provider_type": "custom",
        "credentials": {"base_url": f"{a.mock_url}/{key}/v1", "api_key": "sk-mock"},
        "models": [{"model_id": model_id, "name": model_id, "is_custom": True, "is_enabled": True}],
    }, headers=H)
    r.raise_for_status()
    ids[key] = r.json()["id"]

anthropic_key = os.environ.get("ANTHROPIC_KEY_LOOP")
if anthropic_key:
    if "Anthropic (Haiku 5.5)" in existing:
        ids["anthropic"] = existing["Anthropic (Haiku 5.5)"]["id"]
    else:
        r = c.post("/api/llm/providers", json={
            "name": "Anthropic (Haiku 5.5)", "provider_type": "anthropic",
            "credentials": {"api_key": anthropic_key},
            "models": [{"model_id": "claude-haiku-5-5", "name": "Claude Haiku 5.5", "is_custom": False, "is_enabled": True}],
        }, headers=H)
        if r.status_code >= 400:
            raise SystemExit(f"creating the Anthropic provider failed: HTTP {r.status_code}")
        ids["anthropic"] = r.json()["id"]

models = c.get("/api/llm/models", headers=H).json()
for m in models:
    if m.get("model_id") == "gpt-public":
        c.post(f"/api/llm/models/{m['id']}/set_default", headers=H).raise_for_status()
        c.post(f"/api/llm/models/{m['id']}/set_default", params={"small": True}, headers=H)

default_policy = {"name": "Default", "mode": "replace"}
if a.tokens:
    # Hide emails from the model only (people still see them), and do the same
    # for names when this instance has a detector.
    default_policy["builtin_overrides"] = {"email": {"action": "pseudonymize"}}
    status = c.get("/api/organization/pii/builtin-rules", headers=H).json().get("detector") or {}
    if status.get("available"):
        default_policy["detector_overrides"] = {"PERSON": {"enabled": True, "action": "pseudonymize"}}
        default_policy["on_detector_error"] = a.on_detector_error
r = c.put("/api/organization/settings", json={"config": {"pii_protection": {
    "enabled": True,
    "custom_rules": [{"id": "emp", "name": "Employee ID", "patterns": [r"EMP-\d{4}"], "replacement": "[EMP]"}],
    "policies": {
        "default": default_policy,
        "strict": {"name": "Strict", "mode": "block"},
    },
    "provider_policies": {ids["strict"]: "strict"},
}}}, headers=H)
if r.status_code >= 400:
    raise SystemExit(f"saving PII settings failed: HTTP {r.status_code} {r.text[:300]}")

INSTRUCTION = "When a question is about refunds, tell the user to contact billing-escalations@musicstore.example."
have = c.get("/api/instructions", headers=H, params={"limit": 200})
texts = []
if have.status_code == 200:
    body = have.json()
    items = body.get("items", body) if isinstance(body, dict) else body
    texts = [i.get("text", "") for i in items if isinstance(i, dict)]
if INSTRUCTION not in texts:
    r = c.post("/api/instructions", json={"text": INSTRUCTION, "status": "published", "category": "general"}, headers=H)
    if r.status_code >= 400:
        print(f"warning: instruction not created (HTTP {r.status_code}): {r.text[:200]}")

print(json.dumps({"org_id": org, **ids}))
