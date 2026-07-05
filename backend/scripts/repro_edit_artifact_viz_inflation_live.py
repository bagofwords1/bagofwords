#!/usr/bin/env python3
"""Live (Loop B) reproduction of the edit_artifact visualization_ids
inflation leak, against a running bagofwords stack with the Chinook demo
data source and a REAL LLM.

What it does (viz == query, 1:1):
  1. registers/logs in an admin, creates an org
  2. installs an Anthropic LLM provider (key from env — see below)
  3. installs the chinook demo data source
  4. creates a report and drives the real chat agent through 3 turns:
       T1  create 3 visualizations + a dashboard artifact from them
       T2  create 7 MORE visualizations (explicitly: don't touch the dashboard)
       T3  a cosmetic dashboard edit (rename the title) -> planner calls
           edit_artifact, whose auto-merge sweeps in the 7 later vizs
  5. publishes the report, makes the artifact public, and replays the
     public /r page's data waterfall, printing what it loads vs. what the
     artifact code actually references.

Prereqs:
    tools/agent/boot_stack.sh          # or at least the backend on :8000
    export ANTHROPIC_API_KEY_TEST=...  # never hardcode; never printed

Run:
    cd backend && uv run python scripts/repro_edit_artifact_viz_inflation_live.py

Exit code 0 = invariant holds (artifact keeps only used vizs).
Exit code 1 = leak reproduced (edited artifact carries unused vizs).
"""
import json
import os
import re
import sys
import time

import httpx

BASE_URL = os.environ.get("BOW_BASE_URL", "http://localhost:8000")
EMAIL = "leak-admin@example.com"
PASSWORD = "Password123!"
ORG_NAME = "Leak Repro Org"
TURN_TIMEOUT = float(os.environ.get("BOW_TURN_TIMEOUT", "900"))

T1_PROMPT = (
    "Create exactly three visualizations from the music store data: "
    "1) total revenue by country as a bar chart, "
    "2) top 10 albums by revenue as a table, "
    "3) revenue by genre as a pie chart. "
    "Then create a dashboard artifact that uses exactly these three visualizations. "
    "Keep the dashboard simple."
)

T2_PROMPT = (
    "Now create seven MORE visualizations, as seven separate queries: "
    "1) top 10 artists by number of tracks (table), "
    "2) number of customers per country (bar chart), "
    "3) total sales per year (line chart), "
    "4) top 10 customers by total spend (table), "
    "5) average invoice total per billing country (bar chart), "
    "6) number of tracks per media type (pie chart), "
    "7) top 10 longest tracks by duration (table). "
    "IMPORTANT: do NOT modify, edit, recreate or touch the existing dashboard artifact "
    "in any way. Only create the data visualizations."
)

T3_PROMPT = (
    "In the existing dashboard, rename the main title to 'Chinook Executive Overview'. "
    "This is the ONLY change I want: do not add, remove or modify any charts, "
    "and do not create any new visualizations."
)


def die(msg):
    print(f"FATAL: {msg}")
    sys.exit(2)


def auth(token, org_id=None):
    h = {"Authorization": f"Bearer {token}"}
    if org_id:
        h["X-Organization-Id"] = str(org_id)
    return h


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY_TEST") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        die("set ANTHROPIC_API_KEY_TEST in the environment (never hardcode it)")

    client = httpx.Client(base_url=BASE_URL, timeout=60)

    # 1. Admin + org ---------------------------------------------------------
    r = client.post("/api/auth/register", json={"name": "Leak Admin", "email": EMAIL, "password": PASSWORD})
    if r.status_code not in (201, 400):
        die(f"register: {r.status_code} {r.text}")
    r = client.post("/api/auth/jwt/login", data={"username": EMAIL, "password": PASSWORD})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]

    r = client.get("/api/organizations", headers=auth(token))
    orgs = r.json() if r.status_code == 200 else []
    if orgs:
        org_id = orgs[0]["id"]
    else:
        r = client.post("/api/organizations", json={"name": ORG_NAME}, headers=auth(token))
        assert r.status_code == 200, r.text
        org_id = r.json()["id"]
    print(f"[setup] org={org_id}")

    # 2. LLM provider (anthropic default models) -----------------------------
    r = client.get("/api/llm/providers", headers=auth(token, org_id))
    have_anthropic = r.status_code == 200 and any(
        p.get("provider_type") == "anthropic" for p in r.json()
    )
    if not have_anthropic:
        payload = {
            "name": "anthropic provider",
            "provider_type": "anthropic",
            "credentials": {"api_key": api_key},
            "models": [
                {"model_id": "claude-sonnet-5", "name": "Claude Sonnet 5", "is_custom": False},
                {"model_id": "claude-haiku-4-5-20251001", "name": "Claude 4.5 Haiku", "is_custom": False},
            ],
        }
        r = client.post("/api/llm/providers", json=payload, headers=auth(token, org_id))
        assert r.status_code == 200, r.text
    print("[setup] anthropic provider installed")

    # 3. Chinook demo data source --------------------------------------------
    r = client.get("/api/data_sources", headers=auth(token, org_id))
    existing_ds = next(
        (d for d in (r.json() if r.status_code == 200 else []) if "chinook" in json.dumps(d).lower()),
        None,
    )
    if existing_ds:
        ds_id = existing_ds["id"]
    else:
        r = client.post("/api/data_sources/demos/chinook", headers=auth(token, org_id))
        assert r.status_code == 200, r.text
        ds_id = r.json()["data_source_id"]
        assert ds_id, f"demo install failed: {r.json()}"
    print(f"[setup] chinook data source={ds_id}")

    # 4. Report + agent turns --------------------------------------------------
    r = client.post(
        "/api/reports",
        json={"title": "Viz Inflation Repro", "widget": None, "files": [], "data_sources": [ds_id]},
        headers=auth(token, org_id),
    )
    assert r.status_code == 200, r.text
    report_id = r.json()["id"]
    print(f"[setup] report={report_id}")

    def turn(label, prompt):
        t0 = time.time()
        print(f"[{label}] sending prompt…")
        resp = client.post(
            f"/api/reports/{report_id}/completions",
            params={"background": False},
            json={"prompt": {"content": prompt, "widget_id": None, "step_id": None, "mentions": [{}]}},
            headers=auth(token, org_id),
            timeout=TURN_TIMEOUT,
        )
        assert resp.status_code == 200, f"{label}: {resp.status_code} {resp.text[:400]}"
        print(f"[{label}] done in {time.time() - t0:.0f}s")

    def artifacts():
        resp = client.get(f"/api/artifacts/report/{report_id}", headers=auth(token, org_id))
        assert resp.status_code == 200, resp.text
        out = []
        for a in resp.json():
            full = client.get(f"/api/artifacts/{a['id']}", headers=auth(token, org_id))
            assert full.status_code == 200, full.text
            out.append(full.json())
        out.sort(key=lambda a: a.get("version") or 0)
        return out

    turn("T1", T1_PROMPT)
    arts = artifacts()
    if not arts:
        die("T1 produced no artifact — inspect the completion in the UI and retry")
    v1 = arts[-1]
    v1_ids = (v1.get("content") or {}).get("visualization_ids") or []
    print(f"[T1] artifact v{v1.get('version')} visualization_ids={len(v1_ids)}")

    turn("T2", T2_PROMPT)
    arts_after_t2 = artifacts()
    print(f"[T2] artifact versions now: {[a.get('version') for a in arts_after_t2]} "
          f"(should be unchanged from T1)")

    turn("T3", T3_PROMPT)
    arts_final = artifacts()
    v_final = arts_final[-1]
    final_ids = (v_final.get("content") or {}).get("visualization_ids") or []
    code = (v_final.get("content") or {}).get("code") or ""
    referenced = [vid for vid in final_ids if vid in code]
    unreferenced = [vid for vid in final_ids if vid not in code]

    print(f"\n[result] artifact v{v_final.get('version')} after cosmetic edit:")
    print(f"[result]   visualization_ids: {len(final_ids)} "
          f"(was {len(v1_ids)} before the edit)")
    print(f"[result]   referenced by the dashboard code: {len(referenced)}")
    print(f"[result]   NEVER referenced by the code:     {len(unreferenced)}")

    # 5. Public /r page waterfall ---------------------------------------------
    client.post(f"/api/reports/{report_id}/publish", headers=auth(token, org_id))
    client.put(
        f"/api/reports/{report_id}/visibility/artifact",
        json={"visibility": "public"},
        headers=auth(token, org_id),
    )

    r = client.get(f"/api/r/{report_id}/queries", params={"artifact_id": v_final["id"]})
    assert r.status_code == 200, r.text
    public_queries = r.json()
    total_bytes = 0
    used_bytes = 0
    used_query_ids = set()
    for q in public_queries:
        q_viz_ids = [v.get("id") for v in (q.get("visualizations") or [])]
        sr = client.get(f"/api/r/{report_id}/queries/{q['id']}/step")
        assert sr.status_code == 200, sr.text
        total_bytes += len(sr.content)
        if any(vid in code for vid in q_viz_ids):
            used_bytes += len(sr.content)
            used_query_ids.add(q["id"])

    print(f"\n[public] GET /r/{{id}}/queries?artifact_id= returned {len(public_queries)} queries; "
          f"the dashboard code references vizs from {len(used_query_ids)} of them")
    print(f"[public] /step payload the /r page downloads: {total_bytes / 1e3:.1f} kB "
          f"(referenced by the artifact: {used_bytes / 1e3:.1f} kB — "
          f"{total_bytes / max(used_bytes, 1):.1f}x)")

    if unreferenced:
        print(f"\nLEAK REPRODUCED: the cosmetic edit attached {len(unreferenced)} visualization(s) "
              f"the artifact code never references; the public page now loads "
              f"{total_bytes / max(used_bytes, 1):.1f}x the step data it needs.")
        sys.exit(1)
    print("\nINVARIANT HOLDS: edited artifact only carries visualizations its code uses.")
    sys.exit(0)


if __name__ == "__main__":
    main()
