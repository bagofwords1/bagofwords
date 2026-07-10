#!/usr/bin/env python3
"""Drive a real multi-source completion through the running stack and measure
tool-execution overlap. The verification leg of the concurrent multi-tool
dispatch feedback loop (docs/feedback-loops/concurrent-multi-tool-execution.md).

Prereqs (all through the real HTTP API — this script seeds what's missing):
  1. Stack running:      tools/agent/boot_stack.sh
  2. Sources + admin:    seed_org.py --sqlite-sources N  (or --seed here)
  3. LLM provider: pass --stub-base-url http://127.0.0.1:9099/v1 (stub_llm.py)
     OR --anthropic to build an Anthropic provider from $ANTHROPIC_API_KEY
     (Haiku; the real-model soak leg).

Usage examples:
  uv run python ../tools/agent/run_concurrency_probe.py --stub --iterations 3
  ANTHROPIC_API_KEY=... uv run python ../tools/agent/run_concurrency_probe.py --anthropic

Outputs a per-completion timeline of tool_executions (started_at→finished)
and asserts/reports whether executions overlapped.
"""
import argparse
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import time

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "backend" / "db" / "agent.db"


def auth(token, org_id=None):
    h = {"Authorization": f"Bearer {token}"}
    if org_id:
        h["X-Organization-Id"] = str(org_id)
    return h


def _env(name, *fallbacks, required=True):
    for n in (name, *fallbacks):
        v = os.environ.get(n, "")
        if v:
            return v
    if required:
        sys.exit(f"{name} is not set")
    return ""


def provider_spec(provider, stub_base_url=None):
    """Provider create-payload from env keys. Model ids overridable via env."""
    if provider == "anthropic":
        return {
            "provider_type": "anthropic",
            "credentials": {"api_key": _env("ANTHROPIC_API_KEY")},
            "model_id": os.environ.get("ANTHROPIC_MODEL_ID", "claude-haiku-4-5-20251001"),
            "model_name": "Claude Haiku 4.5",
        }
    if provider == "openai":
        return {
            "provider_type": "openai",
            "credentials": {"api_key": _env("OPENAI_API_KEY")},
            "model_id": os.environ.get("OPENAI_MODEL_ID", "gpt-5.4-mini"),
            "model_name": "GPT-5.4 Mini",
        }
    if provider == "google":
        return {
            "provider_type": "google",
            "credentials": {"api_key": _env("GEMINI_API_KEY", "GOOGLE_API_KEY")},
            "model_id": os.environ.get("GEMINI_MODEL_ID", "gemini-2.5-flash"),
            "model_name": "Gemini 2.5 Flash",
        }
    if provider == "bedrock":
        return {
            "provider_type": "bedrock",
            "credentials": {
                "region": os.environ.get("AWS_BEDROCK_REGION", "us-east-1"),
                "auth_mode": "access_keys",
                "aws_access_key_id": _env("AWS_ACCESS_KEY_ID"),
                "aws_secret_access_key": _env("AWS_SECRET_ACCESS_KEY"),
            },
            "model_id": os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-3-5-haiku-20241022-v1:0"),
            "model_name": "Claude 3.5 Haiku (Bedrock)",
            "is_custom": True,
        }
    if provider == "azure":
        return {
            "provider_type": "azure",
            "credentials": {
                "api_key": _env("AZURE_API_KEY"),
                "endpoint_url": _env("AZURE_ENDPOINT"),
            },
            "model_id": os.environ.get("AZURE_MODEL_ID", "gpt-5.4"),
            "model_name": "Azure GPT",
        }
    if provider == "stub":
        return {
            "provider_type": "openai",
            "credentials": {"api_key": "stub-key", "base_url": stub_base_url},
            "model_id": "gpt-5.4",
            "model_name": "Stub GPT",
        }
    sys.exit(f"unknown provider: {provider}")


def ensure_llm_provider(client, token, org_id, provider, *, stub_base_url=None):
    """Create (if missing) the probe provider for `provider` and make it the
    org default so the completion runs on it."""
    spec = provider_spec(provider, stub_base_url=stub_base_url)
    name = f"{provider} probe provider"
    r = client.get("/api/llm/providers", headers=auth(token, org_id))
    providers = r.json() if r.status_code == 200 else []
    existing = next((p for p in providers if p.get("name") == name), None)
    if existing is None:
        r = client.post("/api/llm/providers", headers=auth(token, org_id), json={
            "name": name,
            "provider_type": spec["provider_type"],
            "credentials": spec["credentials"],
            "models": [{
                "model_id": spec["model_id"],
                "name": spec["model_name"],
                "is_custom": spec.get("is_custom", False),
                "is_default": True,
            }],
        })
        if r.status_code != 200:
            sys.exit(f"{provider} provider create failed: {r.status_code} {r.text}")
        existing = r.json()
    # Make this provider's model the org default (model-level endpoint).
    # Creating a provider with is_default=True does NOT unseat an existing
    # default, and completions run on the org default model.
    r = client.get("/api/llm/models", headers=auth(token, org_id))
    models = r.json() if r.status_code == 200 else []
    target = next(
        (m for m in models
         if m.get("model_id") == spec["model_id"]
         and (m.get("provider") or {}).get("name") == name),
        None,
    ) or next((m for m in models if m.get("model_id") == spec["model_id"]), None)
    if not target:
        sys.exit(f"model {spec['model_id']} not found after provider create")
    r = client.post(f"/api/llm/models/{target['id']}/set_default", headers=auth(token, org_id))
    if r.status_code != 200:
        sys.exit(f"set_default model {spec['model_id']} failed: {r.status_code} {r.text}")
    # Verify: the org default must now be the requested model.
    models = client.get("/api/llm/models", headers=auth(token, org_id)).json()
    default = next((m for m in models if m.get("is_default")), None)
    if not default or default.get("model_id") != spec["model_id"]:
        sys.exit(f"default model is {default and default.get('model_id')}, expected {spec['model_id']}")
    print(f"[probe] provider: {name} (default model {spec['model_id']})")


def set_org_concurrency(client, token, org_id, value):
    """Set the ai_tool_concurrency org setting through the real API — the
    production path for choosing the in-flight cap (the env var is only an
    ops/sandbox override)."""
    r = client.put(
        "/api/organization/settings",
        headers=auth(token, org_id),
        json={"config": {"ai_tool_concurrency": {"value": int(value)}}},
    )
    if r.status_code != 200:
        sys.exit(f"setting ai_tool_concurrency={value} failed: {r.status_code} {r.text}")
    cfg = (r.json().get("config") or {}).get("ai_tool_concurrency") or {}
    print(f"[probe] org setting ai_tool_concurrency={cfg.get('value')}")


def timeline(db_path, completion_created_after):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT te.tool_name, te.status, te.started_at, te.duration_ms, te.success
            FROM tool_executions te
            WHERE te.started_at >= ?
            ORDER BY te.started_at
            """,
            (completion_created_after,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def analyze(rows):
    from datetime import datetime

    windows = []
    for r in rows:
        if not r["started_at"] or r["duration_ms"] is None:
            continue
        try:
            start = datetime.fromisoformat(str(r["started_at"]))
        except ValueError:
            continue
        s = start.timestamp()
        windows.append((r["tool_name"], s, s + (r["duration_ms"] / 1000.0)))
    if not windows:
        return {"tools": 0, "max_overlap": 0, "wall_s": 0.0, "sum_s": 0.0}
    events = []
    for _, s, e in windows:
        events.append((s, 1))
        events.append((e, -1))
    events.sort()
    depth = max_depth = 0
    for _, d in events:
        depth += d
        max_depth = max(max_depth, depth)
    wall = max(e for _, _, e in windows) - min(s for _, s, _ in windows)
    total = sum(e - s for _, s, e in windows)
    return {"tools": len(windows), "max_overlap": max_depth, "wall_s": round(wall, 2), "sum_s": round(total, 2)}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--email", default="admin@example.com")
    p.add_argument("--password", default="Password123!")
    p.add_argument("--db-path", default=str(DEFAULT_DB))
    p.add_argument("--provider", choices=["stub", "anthropic", "openai", "google", "bedrock", "azure"],
                   default=None, help="LLM provider to create/use (credentials from env)")
    p.add_argument("--stub", action="store_true", help="alias for --provider stub")
    p.add_argument("--stub-base-url", default="http://127.0.0.1:9099/v1")
    p.add_argument("--anthropic", action="store_true", help="alias for --provider anthropic")
    p.add_argument("--concurrency", type=int, default=None,
                   help="set the ai_tool_concurrency org setting via the API before running")
    p.add_argument("--iterations", type=int, default=1, help="completions to run")
    p.add_argument("--prompt", default="Inspect the orders table in every connected data source, then create a per-region summary for each source.")
    p.add_argument("--report-id", default=None, help="reuse an existing report instead of creating one")
    args = p.parse_args()

    client = httpx.Client(base_url=args.base_url, timeout=600)
    r = client.post("/api/auth/jwt/login", data={"username": args.email, "password": args.password})
    if r.status_code != 200:
        sys.exit(f"login failed: {r.status_code} {r.text}")
    token = r.json()["access_token"]
    org_id = client.get("/api/organizations", headers=auth(token)).json()[0]["id"]

    provider = args.provider or ("anthropic" if args.anthropic else "stub")
    ensure_llm_provider(client, token, org_id, provider, stub_base_url=args.stub_base_url)
    if args.concurrency is not None:
        set_org_concurrency(client, token, org_id, args.concurrency)

    sources = client.get("/api/data_sources", headers=auth(token, org_id)).json()
    probe_sources = [s for s in sources if s["name"].startswith("sqlite_source_")]
    if not probe_sources:
        sys.exit("no sqlite_source_* data sources — run seed_org.py --sqlite-sources 5 first")
    print(f"[probe] {len(probe_sources)} sources: {[s['name'] for s in probe_sources]}")

    report_id = args.report_id
    if not report_id:
        r = client.post("/api/reports", headers=auth(token, org_id), json={
            "title": "Concurrency probe",
            "widget": None,
            "files": [],
            "data_sources": [s["id"] for s in probe_sources],
        })
        if r.status_code != 200:
            sys.exit(f"report create failed: {r.status_code} {r.text}")
        report_id = r.json()["id"]
    print(f"[probe] report: {report_id}  (env override BOW_AGENT_TOOL_CONCURRENCY={os.environ.get('BOW_AGENT_TOOL_CONCURRENCY', 'unset')})")

    results = []
    for it in range(args.iterations):
        from datetime import datetime, timezone
        # tool_executions.started_at is stored as 'YYYY-MM-DD HH:MM:SS.ffffff'
        # (naive UTC, space separator) — match that shape for the >= filter.
        t0_iso = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")
        started = time.monotonic()
        r = client.post(
            f"/api/reports/{report_id}/completions",
            headers=auth(token, org_id),
            params={"background": True},
            json={"prompt": {"content": args.prompt, "widget_id": None, "step_id": None, "mentions": [{}]}},
        )
        if r.status_code != 200:
            print(f"[probe] iteration {it}: completion failed {r.status_code}: {r.text[:400]}")
            continue
        # Background mode: poll until the system completion leaves in_progress.
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            conn = sqlite3.connect(args.db_path)
            n_open = conn.execute(
                "SELECT COUNT(*) FROM completions WHERE status = 'in_progress' AND created_at >= ?",
                (t0_iso,),
            ).fetchone()[0]
            conn.close()
            if n_open == 0 and time.monotonic() - started > 5:
                break
            time.sleep(3)
        wall = time.monotonic() - started
        rows = timeline(args.db_path, t0_iso)
        stats = analyze(rows)
        stats["completion_wall_s"] = round(wall, 2)
        results.append(stats)
        print(f"[probe] iteration {it}: {json.dumps(stats)}")
        for row in rows:
            print(f"         {row['tool_name']:<14} start={row['started_at']} dur={row['duration_ms']}ms success={row['success']}")

    if results:
        overlapped = [r for r in results if r["max_overlap"] > 1]
        print(f"\n[probe] SUMMARY: {len(results)} completions, "
              f"{len(overlapped)} with overlapping tool executions "
              f"(max depth {max((r['max_overlap'] for r in results), default=0)})")


if __name__ == "__main__":
    main()
