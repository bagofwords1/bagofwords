#!/usr/bin/env python3
"""Standalone sandbox smoke test.

Exercises three routes, repeating each call ``RUNS`` times to collect per-request
latency (in seconds):

1. ``GET  /api/users/whoami``
2. ``GET  /api/data_sources/{ds_id}/test_connection``
3. ``POST /api/llm/test_connection``  — exercises an *unsaved* custom / OpenAI-
   compatible provider pointed at a LiteLLM base URL, so nothing has to be
   persisted first.

All configuration lives in the CONFIG block below. Fill in the required values
before running — the script refuses to start with blanks. Every field can also
be overridden via environment variable.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request


# ============================================================================
# CONFIG — fill these in.  Env vars (in comments) override the inline value.
# ============================================================================

BACKEND_URL       = ""       # BOW_BACKEND_URL          e.g. "http://localhost:8000"
USER_EMAIL        = ""       # BOW_USER_EMAIL           (informational)
AUTH_TOKEN        = ""       # BOW_AUTH_TOKEN           bearer JWT
ORG_ID            = ""       # BOW_ORG_ID
DS_ID             = ""       # BOW_DS_ID                data source id

LLM_PROVIDER_TYPE = ""       # BOW_LLM_PROVIDER_TYPE    usually "custom"
LLM_BASE_URL      = ""       # BOW_LLM_BASE_URL         e.g. "https://litellm.internal"
LLM_API_KEY       = ""       # BOW_LLM_API_KEY
LLM_MODEL_ID      = ""       # BOW_LLM_MODEL_ID         e.g. "claude-sonnet-4-6"
LLM_VERIFY_SSL    = False    # BOW_LLM_VERIFY_SSL       "1"/"true" to re-enable

RUNS              = 10       # RUNS

# ============================================================================


def _coerce_bool(env_value: str | None, default: bool) -> bool:
    if env_value is None or env_value == "":
        return default
    return env_value.strip().lower() not in {"0", "false", "no", "off"}


def resolve_config() -> dict:
    def pick(env_key: str, inline):
        v = os.environ.get(env_key)
        return v if v not in (None, "") else inline

    cfg = {
        "backend":           pick("BOW_BACKEND_URL",       BACKEND_URL),
        "email":             pick("BOW_USER_EMAIL",        USER_EMAIL),
        "token":             pick("BOW_AUTH_TOKEN",        AUTH_TOKEN),
        "org_id":            pick("BOW_ORG_ID",            ORG_ID),
        "ds_id":             pick("BOW_DS_ID",             DS_ID),
        "llm_provider_type": pick("BOW_LLM_PROVIDER_TYPE", LLM_PROVIDER_TYPE),
        "llm_base_url":      pick("BOW_LLM_BASE_URL",      LLM_BASE_URL),
        "llm_api_key":       pick("BOW_LLM_API_KEY",       LLM_API_KEY),
        "llm_model_id":      pick("BOW_LLM_MODEL_ID",      LLM_MODEL_ID),
        "llm_verify_ssl":    _coerce_bool(os.environ.get("BOW_LLM_VERIFY_SSL"), LLM_VERIFY_SSL),
        "runs":              int(os.environ.get("RUNS") or RUNS),
    }

    required = [
        "backend", "token", "org_id", "ds_id",
        "llm_provider_type", "llm_base_url", "llm_api_key", "llm_model_id",
    ]
    missing = [k for k in required if not cfg[k]]
    if missing:
        sys.exit(f"missing required config: {missing}. Edit CONFIG at top of this file or set the matching env vars.")

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
            elapsed_s = time.perf_counter() - start
            try:
                payload = json.loads(raw or "{}")
            except json.JSONDecodeError:
                payload = {"raw": raw}
            return resp.status, payload, elapsed_s
    except urllib.error.HTTPError as exc:
        elapsed_s = time.perf_counter() - start
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": raw}
        return exc.code, payload, elapsed_s


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1)))))
    return ordered[k]


def run_probe(name: str, n: int, call) -> dict:
    """`call()` must return `(ok: bool, status: int, detail: str, elapsed_s: float)`."""
    runs: list[dict] = []
    for i in range(1, n + 1):
        ok, status, detail, elapsed = call()
        runs.append({"i": i, "ok": ok, "status": status, "detail": detail, "s": elapsed})

    timings = [r["s"] for r in runs]
    passed = sum(1 for r in runs if r["ok"])
    return {
        "name": name,
        "runs": runs,
        "passed": passed,
        "total": n,
        "min_s": min(timings),
        "max_s": max(timings),
        "avg_s": statistics.fmean(timings),
        "p50_s": percentile(timings, 50),
        "p95_s": percentile(timings, 95),
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
            f"  latency (s): "
            f"min={p['min_s']:.3f}  "
            f"avg={p['avg_s']:.3f}  "
            f"p50={p['p50_s']:.3f}  "
            f"p95={p['p95_s']:.3f}  "
            f"max={p['max_s']:.3f}"
        )
        for r in p["runs"]:
            mark = "PASS" if r["ok"] else "FAIL"
            print(f"  [{mark}] run {r['i']:>2}/{p['total']}  status={r['status']}  {r['s']:>7.3f} s  {r['detail']}")

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
    llm_provider_type = cfg["llm_provider_type"]
    llm_base_url = cfg["llm_base_url"]
    llm_api_key = cfg["llm_api_key"]
    llm_model_id = cfg["llm_model_id"]
    llm_verify_ssl = cfg["llm_verify_ssl"]
    runs = cfg["runs"]

    print(f"backend={backend}  user={cfg['email']}  org={org_id}  ds={ds_id}")
    print(f"llm_probe={llm_provider_type}/{llm_model_id}  base_url={llm_base_url}  verify_ssl={llm_verify_ssl}  runs={runs}")

    def call_whoami():
        status, body, s = request(
            "GET", f"{backend}/api/users/whoami", token=token, org_id=org_id
        )
        ok = status == 200 and bool(body.get("email"))
        return ok, status, f"email={body.get('email')!r}", s

    def call_ds():
        status, body, s = request(
            "GET",
            f"{backend}/api/data_sources/{ds_id}/test_connection",
            token=token,
            org_id=org_id,
        )
        ok = status == 200 and body.get("success") is True
        msg = body.get("message") or body.get("error") or body.get("detail")
        return ok, status, f"message={msg!r}", s

    # LLM probe: fully inline unsaved provider. For "custom" the body must carry
    # base_url (+ verify_ssl); for other types api_key is the main field.
    def _build_credentials():
        if llm_provider_type == "custom":
            creds = {"base_url": llm_base_url, "verify_ssl": llm_verify_ssl}
            if llm_api_key:
                creds["api_key"] = llm_api_key
            return creds
        return {"api_key": llm_api_key}

    llm_payload = {
        "name": f"probe-{llm_provider_type}",
        "provider_type": llm_provider_type,
        "credentials": _build_credentials(),
        "models": [{"model_id": llm_model_id, "name": llm_model_id, "is_default": True, "is_enabled": True}],
    }

    def call_llm():
        status, body, s = request(
            "POST",
            f"{backend}/api/llm/test_connection",
            token=token,
            org_id=org_id,
            body=llm_payload,
        )
        ok = status == 200 and body.get("success") is True
        msg = body.get("message") or body.get("error") or body.get("detail")
        return ok, status, f"message={msg!r}", s

    probes = [
        run_probe("whoami", runs, call_whoami),
        run_probe(f"data_source test_connection ({ds_id})", runs, call_ds),
        run_probe(f"llm test_connection ({llm_provider_type} -> {llm_model_id})", runs, call_llm),
    ]

    print_report(probes, runs)
    return 0 if all(p["passed"] == p["total"] for p in probes) else 1


if __name__ == "__main__":
    sys.exit(main())
