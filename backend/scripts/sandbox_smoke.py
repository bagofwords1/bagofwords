#!/usr/bin/env python3
"""Sandbox smoke test.

Reads session state from ``backend/sandbox_state.json`` and exercises three
routes, repeating each call ``RUNS`` times to collect per-request latency:

1. ``GET /api/users/whoami``
2. ``GET /api/data_sources/{ds_id}/test_connection``
3. ``POST /api/llm/test_connection`` for the configured provider id (credentials
   must be re-supplied — the GET provider route never returns them).

Usage:
    python backend/scripts/sandbox_smoke.py
    ANTHROPIC_API_KEY=sk-ant-... python backend/scripts/sandbox_smoke.py
    RUNS=25 python backend/scripts/sandbox_smoke.py

Exits non-zero if any check fails.
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


REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = REPO_ROOT / "backend" / "sandbox_state.json"
RUNS = int(os.environ.get("RUNS", "10"))


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


def print_report(probes: list[dict]) -> None:
    print()
    print("=" * 78)
    print(f"Sandbox smoke report  (runs per probe: {RUNS})")
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
    state = load_state()
    backend = state["endpoints"]["backend"].rstrip("/")
    token = state["session"]["token"]
    org_id = state["session"]["org_id"]
    ds_id = state["session"]["ds_id"]
    llm_provider_id = state["session"].get("llm_provider_id")
    llm_provider_type = state["session"].get("llm_provider_type", "anthropic")
    llm_model_id = state["session"].get("llm_model_id_str", "claude-sonnet-4-6")

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
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("BOW_LLM_API_KEY")
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
            return False, 0, "no llm_provider_id in sandbox_state.json", 0.0
        if llm_payload is None:
            return False, 0, "ANTHROPIC_API_KEY not set — skipping live probe", 0.0
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
        run_probe("whoami", RUNS, call_whoami),
        run_probe(f"data_source test_connection ({ds_id})", RUNS, call_ds),
        run_probe(f"llm test_connection ({llm_provider_id})", RUNS, call_llm),
    ]

    print_report(probes)
    return 0 if all(p["passed"] == p["total"] for p in probes) else 1


if __name__ == "__main__":
    sys.exit(main())
