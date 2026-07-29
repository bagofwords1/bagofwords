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
        have_haiku = any(str(m.get("model_id", "")).startswith("claude-haiku")
                         for m in models)
        # Note: do NOT return early when the model exists — it may exist while
        # the *mock* is still the org default, which is the exact way this pass
        # would silently re-measure the mock.
        if have_haiku:
            print("real Haiku model already present; ensuring it is default")

        r = None if have_haiku else c.post(f"{base}/api/llm/providers", headers=H, json={
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
        if r is not None:
            if r.status_code >= 400:
                print("provider create failed:", r.status_code, r.text[:400],
                      file=sys.stderr)
                return 1
            print("anthropic provider created")

        # Creating the provider does NOT make its model the org default when a
        # default already exists — the mock stays selected and the "real LLM"
        # pass would silently keep using it. Promote Haiku explicitly.
        enabled = c.get(f"{base}/api/llm/models?is_enabled=true", headers=H).json()
        haiku = next((m for m in enabled
                      if str(m.get("model_id", "")).startswith("claude-haiku")), None)
        if not haiku:
            print("haiku model not found after create", file=sys.stderr)
            return 1
        for small in (False, True):
            r = c.post(f"{base}/api/llm/models/{haiku['id']}/set_default",
                       headers=H, params={"small": str(small).lower()})
            if r.status_code >= 400:
                print(f"set_default(small={small}) failed:",
                      r.status_code, r.text[:200], file=sys.stderr)
                return 1

        enabled = c.get(f"{base}/api/llm/models?is_enabled=true", headers=H).json()
        for m in enabled:
            print(f"  {m.get('model_id')} default={m.get('is_default')} "
                  f"small={m.get('is_small_default')}")
        active = [m for m in enabled if m.get("is_default")]
        if not active or not str(active[0].get("model_id", "")).startswith("claude-haiku"):
            print("ERROR: default model is not real Haiku; aborting so the pass "
                  "cannot silently re-measure the mock", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
