#!/usr/bin/env python
"""End-to-end: configure Anthropic Haiku in BOW, enable web_fetch, and run a
REAL completion through the full agent pipeline (agent_v2 -> planner_v3 ->
web_fetch) so a live model emits a connection-tool `title` that the UI renders.

Drives everything through the real HTTP API. Reads ANTHROPIC_API_KEY from env
(never hard-coded). Prints the report URL for a Playwright screenshot.

Usage:
    ANTHROPIC_API_KEY=... .venv/bin/python scripts/run_haiku_title_completion.py
"""
from __future__ import annotations

import json
import os
import sys
import time

import httpx

BASE = os.environ.get("BASE_URL", "http://localhost:8000")
EMAIL = os.environ.get("SEED_EMAIL", "admin@example.com")
PASSWORD = os.environ.get("SEED_PASSWORD", "Password123!")
HAIKU = "claude-haiku-4-5-20251001"


def main() -> int:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 2

    c = httpx.Client(base_url=BASE, timeout=120)
    c.post("/api/auth/register", json={"name": "Demo Admin", "email": EMAIL, "password": PASSWORD})
    tok = c.post("/api/auth/jwt/login", data={"username": EMAIL, "password": PASSWORD}).json()["access_token"]
    org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
    h = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}

    # 1) Anthropic provider + Haiku model (default) --------------------------
    providers = c.get("/api/llm/providers", headers=h).json()
    prov = next((p for p in providers if p["provider_type"] == "anthropic"), None)
    if not prov:
        prov = c.post("/api/llm/providers", headers=h, json={
            "name": "Anthropic", "provider_type": "anthropic",
            "credentials": {"api_key": key},
            "models": [{
                "name": "Claude Haiku 4.5", "model_id": HAIKU, "is_default": True,
                "context_window_tokens": 200000, "max_output_tokens": 8192,
            }],
        }).json()
        print(f"created provider {prov.get('id','?')[:8]}")
    else:
        c.put(f"/api/llm/providers/{prov['id']}", headers=h, json={"credentials": {"api_key": key}})
        print(f"updated provider {prov['id'][:8]} credentials")

    models = c.get("/api/llm/models", headers=h).json()
    hm = next((m for m in models if m.get("model_id") == HAIKU), None)
    if not hm:
        hm = c.post("/api/llm/models", headers=h, json={
            "name": "Claude Haiku 4.5", "model_id": HAIKU, "provider_id": prov["id"],
            "is_default": True, "context_window_tokens": 200000, "max_output_tokens": 8192,
        }).json()
    mid = hm["id"]
    if not hm.get("is_enabled", False):
        c.post(f"/api/llm/models/{mid}/toggle", headers=h)
    c.post(f"/api/llm/models/{mid}/set_default", headers=h)
    print(f"haiku model {mid[:8]} enabled + default")

    # 2) enable web_fetch ----------------------------------------------------
    settings = c.get("/api/organization/settings", headers=h).json()
    cfg = settings.get("config") or {}
    wf = cfg.get("enable_web_fetch") or {}
    wf["value"] = True
    cfg["enable_web_fetch"] = wf
    r = c.put("/api/organization/settings", headers=h, json={"config": cfg})
    print(f"web_fetch enabled: {r.status_code} -> {(r.json().get('config',{}).get('enable_web_fetch',{}) or {}).get('value')}")

    # 3) report + a real completion -----------------------------------------
    report_id = c.post("/api/reports", headers=h, json={"title": "Haiku live tool-title demo"}).json()["id"]
    prompt = ("Fetch https://example.com and tell me the page's title and what it's for. "
              "Use the web_fetch tool.")
    print(f"posting completion to report {report_id} ...")
    t0 = time.time()
    resp = c.post(f"/api/reports/{report_id}/completions?background=false", headers=h,
                  json={"prompt": {"content": prompt}})
    print(f"completion POST {resp.status_code} in {time.time()-t0:.1f}s")

    # 4) verify a web_fetch tool_execution carries a model-authored title ----
    time.sleep(2)
    comps = c.get(f"/api/reports/{report_id}/completions", headers=h).json()
    comp_list = comps if isinstance(comps, list) else comps.get("completions", comps.get("items", []))
    titles = []
    for comp in comp_list:
        for b in (comp.get("completion_blocks") or []):
            te = b.get("tool_execution")
            if te:
                titles.append((te.get("tool_name"), (te.get("arguments_json") or {}).get("title")))
    print("tool calls + titles:", json.dumps(titles, indent=1))
    print(json.dumps({"report_url": f"http://localhost:3000/reports/{report_id}", "report_id": report_id}))
    ok = any(tn in ("web_fetch", "execute_mcp", "search_mcps") and t for tn, t in titles)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
