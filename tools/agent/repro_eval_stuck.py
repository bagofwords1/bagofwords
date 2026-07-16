#!/usr/bin/env python3
"""Reproduce the "eval run stuck on in_progress" bug through the real API.

Drives ``create_and_execute_background`` (POST /api/tests/runs/batch) with a
stub LLM so the agent runs deterministically with no external services, then
polls the run *status* endpoint (which does NOT finalize) until the agent's
system completion reaches a terminal state.

At that point:
  - buggy backend  -> TestResult stays ``in_progress`` and the run stays
                      ``in_progress`` (nothing finalizes it server-side).
  - fixed backend  -> the server-side finalizer flips the result to
                      ``fail`` and the run to ``error``.

Prints a one-line JSON summary and the run_id (the /evals list URL to shoot).

Usage (backend must be up, stub on :9099):
    cd backend && uv run python ../tools/agent/repro_eval_stuck.py
"""
import json
import os
import sys
import time

import httpx

BASE = os.environ.get("BOW_BASE_URL", "http://localhost:8000")
EMAIL = os.environ.get("BOW_ADMIN_EMAIL", "admin@example.com")
PASSWORD = os.environ.get("BOW_ADMIN_PASSWORD", "Password123!")
STUB_BASE_URL = os.environ.get("STUB_BASE_URL", "http://127.0.0.1:9099/v1")
STUB_MODEL_ID = os.environ.get("STUB_MODEL_ID", "gpt-5.4")


def auth(tok, org):
    return {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}


def main():
    c = httpx.Client(base_url=BASE, timeout=120)
    tok = c.post("/api/auth/jwt/login", data={"username": EMAIL, "password": PASSWORD}).json()["access_token"]
    org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
    H = auth(tok, org)

    # 1. Stub LLM provider as org default.
    name = "stub provider"
    providers = c.get("/api/llm/providers", headers=H).json()
    existing = next((p for p in providers if p.get("name") == name), None)
    if existing is None:
        r = c.post("/api/llm/providers", headers=H, json={
            "name": name,
            "provider_type": "openai",
            "credentials": {"api_key": "stub-key", "base_url": STUB_BASE_URL},
            "models": [{"model_id": STUB_MODEL_ID, "name": "Stub GPT", "is_default": True}],
        })
        if r.status_code != 200:
            sys.exit(f"provider create failed: {r.status_code} {r.text}")
    models = c.get("/api/llm/models", headers=H).json()
    target = next((m for m in models if m.get("model_id") == STUB_MODEL_ID), None)
    if not target:
        sys.exit("stub model not found after create")
    c.post(f"/api/llm/models/{target['id']}/set_default", headers=H)

    # 2. Suite + one case with NO data sources and a completion expectation the
    #    stub's canned answer will NOT satisfy (so the case resolves to fail).
    suite = c.post("/api/tests/suites", headers=H, json={
        "name": f"Stuck-run repro {int(time.time())}",
        "description": "Deterministic repro of eval run stuck on in_progress.",
    }).json()
    case = c.post(f"/api/tests/suites/{suite['id']}/cases", headers=H, json={
        "name": "quarterly revenue breakdown",
        "prompt_json": {"content": "Show the quarterly revenue breakdown for 2025."},
        "expectations_json": {
            "spec_version": 1,
            "order_mode": "flexible",
            "rules": [
                {
                    "type": "field",
                    "target": {"category": "completion", "field": "text"},
                    "matcher": {"type": "text.contains", "value": "quarterly revenue breakdown"},
                }
            ],
        },
        "data_source_ids_json": [],
    }).json()

    # 3. Kick off the background run (create_and_execute_background).
    r = c.post("/api/tests/runs/batch", headers=H, json={
        "case_ids": [case["id"]],
        "trigger_reason": "manual",
    })
    if r.status_code != 200:
        sys.exit(f"batch run failed: {r.status_code} {r.text}")
    run = r.json()
    run_id = run["id"]

    # 4. Poll the *status* endpoint (never the stream) until the agent's system
    #    completion reaches a terminal state. This proves the agent finished;
    #    the run/result status is then whatever the backend left it as.
    def agent_done():
        s = c.get(f"/api/tests/runs/{run_id}/status?limit=10", headers=H).json()
        for it in (s.get("results") or []):
            comps = it.get("completions") or []
            sysc = [x for x in comps if (x.get("role") == "system")]
            if not sysc:
                return False
            latest = sysc[-1]
            if latest.get("status") not in {"success", "error", "stopped"}:
                return False
        return True

    deadline = time.time() + 90
    while time.time() < deadline:
        if agent_done():
            break
        time.sleep(1.5)

    # Give any server-side finalizer a moment to run after the agent finishes.
    time.sleep(4)

    run_after = c.get(f"/api/tests/runs/{run_id}", headers=H).json()
    results = c.get(f"/api/tests/runs/{run_id}/results", headers=H).json()
    print(json.dumps({
        "run_id": run_id,
        "run_status": run_after.get("status"),
        "result_statuses": [x.get("status") for x in results],
        "evals_url": f"http://localhost:3000/evals",
        "run_url": f"http://localhost:3000/evals/runs/{run_id}",
    }))


if __name__ == "__main__":
    main()
