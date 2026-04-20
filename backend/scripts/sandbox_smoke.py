#!/usr/bin/env python3
"""Sandbox smoke test.

Exercises three routes, repeating each call ``RUNS`` times to collect per-request
latency:

1. ``GET /api/users/whoami``
2. ``GET /api/data_sources/{ds_id}/test_connection``
3. ``POST /api/llm/test_connection`` for the configured provider id (credentials
   must be re-supplied — the GET provider route never returns them).

Edit the CONFIG block below to point at a different session, or leave values as
``None`` to fall back to ``backend/sandbox_state.json``. Any field can also be
overridden via environment variable (see each line).
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


# ============================================================================
# CONFIG — edit these values, or leave as-is to read from sandbox_state.json.
# ============================================================================

BACKEND_URL = "http://localhost:8000"          # BOW_BACKEND_URL
USER_EMAIL  = "sandbox@bow.dev"                # BOW_USER_EMAIL (informational)
AUTH_TOKEN  = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI2NjBkZjJlMy02ZDdiLTQxNjUtOTYzZi0wYTM4NTQyYTVhY2UiLCJhdWQiOlsiZmFzdGFwaS11c2VyczphdXRoIl0sImV4cCI6MTc3NzI5MTAxOH0.Eu5ZFOoe1zQo7IHpqCj7eQ0hm-tBnN1crqRvrQY2VM8"  # BOW_AUTH_TOKEN
ORG_ID      = "a629ae64-7e39-42e8-bb71-51f3138b7923"   # BOW_ORG_ID
DS_ID       = "bce065f7-f50e-410f-a7b5-68a2d83cb028"   # BOW_DS_ID

LLM_PROVIDER_ID   = "d5d5a068-2a18-47c3-acd3-b08ea4cabc8e"   # BOW_LLM_PROVIDER_ID
LLM_PROVIDER_TYPE = "anthropic"                              # BOW_LLM_PROVIDER_TYPE
LLM_MODEL_ID      = "claude-sonnet-4-6"                      # BOW_LLM_MODEL_ID
LLM_API_KEY       = None  # required for live LLM probe. ANTHROPIC_API_KEY / BOW_LLM_API_KEY env also honored.

RUNS = 10                                       # RUNS

# ============================================================================


REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = REPO_ROOT / "backend" / "sandbox_state.json"


def _load_state_fallback() -> dict:
    """sandbox_state.json is only read for fields left blank in CONFIG."""
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def resolve_config() -> dict:
    """Merge CONFIG + env overrides + sandbox_state.json fallback."""
    state = _load_state_fallback()
    sess = state.get("session", {}) or {}
    endpoints = state.get("endpoints", {}) or {}

    def pick(env_key: str, inline, state_val):
        return os.environ.get(env_key) or inline or state_val

    cfg = {
        "backend":           pick("BOW_BACKEND_URL",      BACKEND_URL, endpoints.get("backend")),
        "email":             pick("BOW_USER_EMAIL",       USER_EMAIL,  (state.get("credentials") or {}).get("email")),
        "token":             pick("BOW_AUTH_TOKEN",       AUTH_TOKEN,  sess.get("token")),
        "org_id":            pick("BOW_ORG_ID",           ORG_ID,      sess.get("org_id")),
        "ds_id":             pick("BOW_DS_ID",            DS_ID,       sess.get("ds_id")),
        "llm_provider_id":   pick("BOW_LLM_PROVIDER_ID",  LLM_PROVIDER_ID,   sess.get("llm_provider_id")),
        "llm_provider_type": pick("BOW_LLM_PROVIDER_TYPE", LLM_PROVIDER_TYPE, sess.get("llm_provider_type") or "anthropic"),
        "llm_model_id":      pick("BOW_LLM_MODEL_ID",     LLM_MODEL_ID, sess.get("llm_model_id_str") or "claude-sonnet-4-6"),
        "api_key":           os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("BOW_LLM_API_KEY") or LLM_API_KEY,
        "runs":              int(os.environ.get("RUNS") or RUNS),
    }

    missing = [k for k in ("backend", "token", "org_id", "ds_id") if not cfg[k]]
    if missing:
        sys.exit(f"missing required config: {missing}. Edit CONFIG at top of {__file__} or populate sandbox_state.json.")

    cfg["backend"] = cfg["backend"].rstrip("/")
    return cfg


def request(
    method: str,
    url: str,
    *,
    token: str,
    org_id: str,
    body: dict | None = None,
) -> tuple[int, dict, float]:
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": org_id,
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            elapsed_ms = (time.perf_counter() - start) * 1000
            try:
                payload = json.loads(raw or "{}")
            except json.JSONDecodeError:
                payload = {"raw": raw}
            return resp.status, payload, elapsed_ms
    except urllib.error.HTTPError as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": raw}
        return exc.code, payload, elapsed_ms


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile — fine for n=10."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1)))))
    return ordered[k]


def run_probe(name: str, n: int, call) -> dict:
    """Invoke `call()` n times; return aggregate + per-run details.

    `call()` must return `(ok: bool, status: int, detail: str, elapsed_ms: float)`.
    """
    runs: list[dict] = []
    for i in range(1, n + 1):
        ok, status, detail, elapsed = call()
        runs.append({"i": i, "ok": ok, "status": status, "detail": detail, "ms": elapsed})

    timings = [r["ms"] for r in runs]
    passed = sum(1 for r in runs if r["ok"])
    return {
        "name": name,
        "runs": runs,
        "passed": passed,
        "total": n,
        "min_ms": min(timings),
        "max_ms": max(timings),
        "avg_ms": statistics.fmean(timings),
        "p50_ms": percentile(timings, 50),
        "p95_ms": percentile(timings, 95),
    }


def print_report(probes: list[dict], runs: int) -> None:
    print()
    print("=" * 78)
    print(f"Sandbox smoke report  (runs per probe: {runs})")
    print("=" * 78)

    for p in probes:
        header = f"{p['name']}  —  {p['passed']}/{p['total']} passed"
        print(f"\n{header}")
        print("-" * len(header))
        print(
            f"  latency (ms): "
            f"min={p['min_ms']:.1f}  "
            f"avg={p['avg_ms']:.1f}  "
            f"p50={p['p50_ms']:.1f}  "
            f"p95={p['p95_ms']:.1f}  "
            f"max={p['max_ms']:.1f}"
        )
        for r in p["runs"]:
            mark = "PASS" if r["ok"] else "FAIL"
            print(f"  [{mark}] run {r['i']:>2}/{p['total']}  status={r['status']}  {r['ms']:>7.1f} ms  {r['detail']}")

    print()
    print("=" * 78)
    all_pass = all(p["passed"] == p["total"] for p in probes)
    summary = "OK" if all_pass else "FAILURES PRESENT"
    print(f"Overall: {summary}")
    print("=" * 78)


def main() -> int:
    cfg = resolve_config()
    backend = cfg["backend"]
    token = cfg["token"]
    org_id = cfg["org_id"]
    ds_id = cfg["ds_id"]
    llm_provider_id = cfg["llm_provider_id"]
    llm_provider_type = cfg["llm_provider_type"]
    llm_model_id = cfg["llm_model_id"]
    api_key = cfg["api_key"]
    runs = cfg["runs"]

    print(f"backend={backend}  user={cfg['email']}  org={org_id}  ds={ds_id}")
    print(f"llm_provider={llm_provider_id} ({llm_provider_type}/{llm_model_id})  runs={runs}")

    # 1. whoami
    def call_whoami():
        status, body, ms = request(
            "GET", f"{backend}/api/users/whoami", token=token, org_id=org_id
        )
        ok = status == 200 and bool(body.get("email"))
        return ok, status, f"email={body.get('email')!r}", ms

    # 2. data source test_connection by id
    def call_ds():
        status, body, ms = request(
            "GET",
            f"{backend}/api/data_sources/{ds_id}/test_connection",
            token=token,
            org_id=org_id,
        )
        ok = status == 200 and body.get("success") is True
        msg = body.get("message") or body.get("error") or body.get("detail")
        return ok, status, f"message={msg!r}", ms

    # 3. llm test_connection — re-supplies credentials because the provider
    #    GET route never returns them. The provider_id is logged so a failure
    #    is traceable back to a specific configured provider.
    llm_payload = None
    if llm_provider_id and api_key:
        llm_payload = {
            "name": f"probe-{llm_provider_id}",
            "provider_type": llm_provider_type,
            "credentials": {"api_key": api_key},
            "models": [{"model_id": llm_model_id, "name": llm_model_id, "is_default": True, "is_enabled": True}],
        }

    def call_llm():
        if not llm_provider_id:
            return False, 0, "LLM_PROVIDER_ID not configured", 0.0
        if llm_payload is None:
            return False, 0, "api key not set — skipping live probe", 0.0
        status, body, ms = request(
            "POST",
            f"{backend}/api/llm/test_connection",
            token=token,
            org_id=org_id,
            body=llm_payload,
        )
        ok = status == 200 and body.get("success") is True
        msg = body.get("message") or body.get("error") or body.get("detail")
        return ok, status, f"message={msg!r}", ms

    probes = [
        run_probe("whoami", runs, call_whoami),
        run_probe(f"data_source test_connection ({ds_id})", runs, call_ds),
        run_probe(f"llm test_connection ({llm_provider_id})", runs, call_llm),
    ]

    print_report(probes, runs)
    return 0 if all(p["passed"] == p["total"] for p in probes) else 1


if __name__ == "__main__":
    sys.exit(main())
