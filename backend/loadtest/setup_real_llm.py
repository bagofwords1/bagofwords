#!/usr/bin/env python3
"""Point the seeded org at real Claude Haiku for the validation pass.

Creates (or reuses) an Anthropic provider with Haiku as the org's default and
small-default model, so the same harness exercises the same flow with a real
provider instead of the mock. Leaves the mock provider in place but disabled,
so switching back is a one-liner.
"""
import json
import os
import sys
import time

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = json.load(open(os.path.join(HERE, "sandbox_state.json")))
KEY = os.environ.get("ANTHROPIC_KEY", "")
CLIENT_KW = dict(timeout=120.0, trust_env=False)


def main():
    if not KEY:
        print("ANTHROPIC_KEY not set", file=sys.stderr)
        return 1
    base, org = STATE["base"], STATE["org_id"]
    with httpx.Client(**CLIENT_KW) as c:
        tok = c.post(f"{base}/api/auth/jwt/login",
                     data={"username": STATE["admin_email"],
                           "password": STATE["password"]}).json()["access_token"]
        H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org,
             "Content-Type": "application/json"}

        models = c.get(f"{base}/api/llm/models?is_enabled=true", headers=H).json()
        if any(m.get("model_id", "").startswith("claude-haiku") for m in models):
            print("real Haiku model already enabled")
            return 0

        r = c.post(f"{base}/api/llm/providers", headers=H, json={
            "name": f"Anthropic-Haiku-{int(time.time())}",
            "provider_type": "anthropic",
            "credentials": {"api_key": KEY},
            "models": [{
                "name": "Claude 4.5 Haiku",
                "model_id": "claude-haiku-4-5-20251001",
                "is_default": True, "is_small_default": True,
                "context_window_tokens": 200000,
                "input_cost_per_million_tokens_usd": 1,
                "output_cost_per_million_tokens_usd": 5,
            }],
        })
        if r.status_code >= 400:
            print("provider create failed:", r.status_code, r.text[:400],
                  file=sys.stderr)
            return 1
        print("anthropic provider created")

        enabled = c.get(f"{base}/api/llm/models?is_enabled=true", headers=H).json()
        for m in enabled:
            print(f"  {m.get('model_id')} default={m.get('is_default')} "
                  f"small={m.get('is_small_default')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
