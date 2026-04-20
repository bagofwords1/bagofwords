#!/usr/bin/env python3
"""Sandbox smoke test.

Reads session state from ``backend/sandbox_state.json`` and exercises three
routes:

1. ``GET /api/data_sources/{ds_id}/test_connection``
2. ``POST /api/llm/test_connection`` for the configured provider id (credentials
   must be re-supplied — the GET provider route never returns them).
3. ``GET /api/users/whoami``

Usage:
    python backend/scripts/sandbox_smoke.py
    ANTHROPIC_API_KEY=sk-ant-... python backend/scripts/sandbox_smoke.py

Exits non-zero if any check fails.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = REPO_ROOT / "backend" / "sandbox_state.json"


def load_state() -> dict:
    if not STATE_PATH.exists():
        sys.exit(f"sandbox_state.json not found at {STATE_PATH}")
    return json.loads(STATE_PATH.read_text())


def request(
    method: str,
    url: str,
    *,
    token: str,
    org_id: str,
    body: dict | None = None,
) -> tuple[int, dict]:
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": org_id,
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
            return resp.status, payload
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": raw}
        return exc.code, payload


def check(name: str, ok: bool, detail: str) -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}: {detail}")
    return ok


def main() -> int:
    state = load_state()
    backend = state["endpoints"]["backend"].rstrip("/")
    token = state["session"]["token"]
    org_id = state["session"]["org_id"]
    ds_id = state["session"]["ds_id"]
    llm_provider_id = state["session"].get("llm_provider_id")
    llm_provider_type = state["session"].get("llm_provider_type", "anthropic")
    llm_model_id = state["session"].get("llm_model_id_str", "claude-sonnet-4-6")

    results: list[bool] = []

    # 1. whoami (run first to catch stale tokens)
    status, body = request("GET", f"{backend}/api/users/whoami", token=token, org_id=org_id)
    results.append(check(
        "whoami",
        status == 200 and body.get("email"),
        f"status={status} email={body.get('email')!r}",
    ))

    # 2. data source test_connection
    status, body = request(
        "GET",
        f"{backend}/api/data_sources/{ds_id}/test_connection",
        token=token,
        org_id=org_id,
    )
    results.append(check(
        f"data_source test_connection ({ds_id})",
        status == 200 and body.get("success") is True,
        f"status={status} message={body.get('message')!r}",
    ))

    # 3. llm test_connection — re-supplies credentials because the provider
    #    GET route never returns them. The provider_id is included so a failure
    #    is traceable back to a specific configured provider.
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("BOW_LLM_API_KEY")
    if not llm_provider_id:
        results.append(check("llm test_connection", False, "no llm_provider_id in sandbox_state.json"))
    elif not api_key:
        results.append(check(
            f"llm test_connection ({llm_provider_id})",
            False,
            "ANTHROPIC_API_KEY not set — skipping live probe",
        ))
    else:
        payload = {
            "name": f"probe-{llm_provider_id}",
            "provider_type": llm_provider_type,
            "credentials": {"api_key": api_key},
            "models": [{"model_id": llm_model_id, "name": llm_model_id, "is_default": True, "is_enabled": True}],
        }
        status, body = request(
            "POST",
            f"{backend}/api/llm/test_connection",
            token=token,
            org_id=org_id,
            body=payload,
        )
        results.append(check(
            f"llm test_connection ({llm_provider_id})",
            status == 200 and body.get("success") is True,
            f"status={status} message={body.get('message')!r}",
        ))

    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
