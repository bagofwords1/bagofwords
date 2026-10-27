#!/usr/bin/env python3
"""Live loop: an Auto chat reads a ServiceNow KB attachment as page images on
Bedrock / Gemini — through the real backend, real provider clients, real
read_file rendering (LibreOffice), with only the network edges faked.

Pieces (all local, no credentials):
  backend                  python main.py, started with
                           AWS_ENDPOINT_URL_BEDROCK_RUNTIME / GOOGLE_GEMINI_BASE_URL
                           pointed at the bridge
  tools/agent/fake_servicenow.py      KB0013962 + its .docx (text + screenshot)
  tools/agent/provider_bridge_llm.py  Bedrock / Gemini wire protocol; scripted
                           agent that OCRs the images it actually receives

The chat is created with NO agents attached (Auto). The run must:
  1. offer read_file to the model            (Auto tool gap — fix 1)
  2. resolve the ServiceNow file source       (Auto resolver gap — fix 1)
  3. deliver the rendered page images to the provider (transcript images on
     Bedrock / Gemini — fix 2), so the screenshot-only code is read back.

    cd backend && uv run python ../tools/agent/verify_auto_file_tools_vision.py --provider bedrock
    cd backend && uv run python ../tools/agent/verify_auto_file_tools_vision.py --provider gemini

Exit code 0 = all three hold; otherwise prints which one broke.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import uuid

import httpx

SCREEN_CODE = "ZEBRA-7731"  # only inside the screenshot — see fake_servicenow.py
SOURCE = "ServiceNow KB"

PROVIDERS = {
    "bedrock": {
        "provider_type": "bedrock",
        "credentials": {"region": "us-east-1", "auth_mode": "access_keys",
                        "aws_access_key_id": "AKIAFAKEFAKEFAKE", "aws_secret_access_key": "fake"},
        "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "is_custom": True,
    },
    "gemini": {
        "provider_type": "google",
        "credentials": {"api_key": "fake-gemini-key"},
        "model_id": "gemini-3.6-flash",
        "is_custom": False,
    },
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=sorted(PROVIDERS), required=True)
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--bridge-url", default="http://127.0.0.1:9312")
    ap.add_argument("--servicenow-url", default="http://127.0.0.1:9311")
    ap.add_argument("--db-path", default="db/agent.db")
    ap.add_argument("--email", default="admin@example.com")
    ap.add_argument("--password", default="Password123!")
    ap.add_argument("--attach", action="store_true",
                    help="attach the agent to the chat (manual selection) instead of Auto — "
                         "isolates the image path from the Auto tool gap")
    args = ap.parse_args()

    c = httpx.Client(base_url=args.base_url, timeout=600)
    bridge = httpx.Client(base_url=args.bridge_url, timeout=30)
    tok = c.post("/api/auth/jwt/login", data={"username": args.email, "password": args.password}).json()["access_token"]
    org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
    H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}

    # Keep the run to the planner loop: no judge / follow-ups / suggestions.
    r = c.put("/api/organization/settings", headers=H, json={"config": {
        "suggest_instructions": {"value": False}, "enable_follow_ups": {"value": False},
        "enable_llm_judgement": {"value": False}, "ai_tool_concurrency": {"value": 1},
        "agent_max_steps": {"value": 8},
    }})
    assert r.status_code == 200, r.text

    # Provider + default model (vision on).
    spec = PROVIDERS[args.provider]
    name = f"{args.provider} bridge provider"
    providers = c.get("/api/llm/providers", headers=H).json()
    prov = next((p for p in providers if p.get("name") == name), None)
    if prov is None:
        r = c.post("/api/llm/providers", headers=H, json={
            "name": name, "provider_type": spec["provider_type"], "credentials": spec["credentials"],
            "models": [{"model_id": spec["model_id"], "name": f"{args.provider} vision",
                        "is_custom": spec["is_custom"], "is_default": True,
                        "supports_vision_override": True}],
        })
        assert r.status_code == 200, f"provider create: {r.status_code} {r.text}"
    else:  # ephemeral encryption keys: refresh stored credentials each run
        c.put(f"/api/llm/providers/{prov['id']}", headers=H, json={"credentials": spec["credentials"], "models": []})
    model = next(m for m in c.get("/api/llm/models", headers=H).json()
                 if m.get("model_id") == spec["model_id"] and (m.get("provider") or {}).get("name") == name)
    assert c.post(f"/api/llm/models/{model['id']}/set_default", headers=H).status_code == 200
    if not model.get("supports_vision"):
        r = c.post(f"/api/llm/models/{model['id']}/toggle_vision", headers=H, params={"enabled": True})
        assert r.status_code == 200, f"toggle_vision: {r.status_code} {r.text}"

    # ServiceNow agent (attachments on by default).
    ds = next((d for d in c.get("/api/data_sources", headers=H).json() if d.get("name") == SOURCE), None)
    if ds is None:
        r = c.post("/api/data_sources", headers=H, json={
            "name": SOURCE, "type": "servicenow",
            "config": {"instance_url": args.servicenow_url, "tables": "kb_knowledge",
                       "infer_schema_from_data": True},
            "credentials": {"username": "integration", "password": "x"},
            "auth_policy": "system_only",
        })
        assert r.status_code in (200, 201), f"data source create: {r.status_code} {r.text}"
        ds = r.json()
    print(f"[loop] provider={args.provider} model={spec['model_id']} agent={ds['name']} ({ds['id']})")

    # Auto chat (no agents attached) unless --attach.
    r = c.post("/api/reports", headers=H, json={"title": f"KB images {uuid.uuid4().hex[:6]}",
                                                 "widget": None, "files": [],
                                                 "data_sources": [ds["id"]] if args.attach else []})
    assert r.status_code == 200, r.text
    report = r.json()
    assert bool(report.get("data_sources")) == args.attach, "unexpected agent selection on the chat"
    print(f"[loop] chat selection: {'manual (agent attached)' if args.attach else 'Auto (no agents attached)'}")
    bridge.post("/reset")

    t0 = time.time()
    r = c.post(f"/api/reports/{report['id']}/completions", headers=H, params={"background": False}, json={
        "prompt": {"content": "Read the KB0013962 procedure document with read_file and read its images. "
                              "What routing code does the screenshot show?",
                   "widget_id": None, "step_id": None, "mentions": [{}]}})
    assert r.status_code == 200, f"completion: {r.status_code} {r.text[:400]}"
    run = _latest_run(args.db_path, report["id"])
    stats = bridge.get("/stats").json()
    answer = str(json.loads(run["completion"]["completion"] or "{}").get("content") or "")

    offered = [("read_file" in names) for names in stats["planner_tool_names"]]
    tools = [(t["tool_name"], bool(t["success"])) for t in run["tools"]]
    images = stats[args.provider]["images_per_request"]
    checks = {
        "1. read_file offered to the model": bool(offered) and all(offered),
        "2. read_file resolved the ServiceNow file": ("read_file", True) in tools,
        "3. page images reached the provider": max(images or [0]) > 0,
        f"3b. model read the screenshot code {SCREEN_CODE}": SCREEN_CODE in answer.replace(" ", ""),
    }
    print(f"[loop] {time.time() - t0:.1f}s  tools={tools}")
    print(f"[loop] images per {args.provider} request: {images}")
    flat = " ".join(answer.split())
    at = flat.find("ROUTING")
    print(f"[loop] answer: {flat[:160]}{' … ' + flat[at:at + 80] if at > 160 else ''}")
    for k, ok in checks.items():
        print(f"[loop] {'PASS' if ok else 'FAIL'}  {k}")
    return 0 if all(checks.values()) else 1


def _latest_run(db_path: str, report_id: str) -> dict:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        comp = con.execute("SELECT id, status, completion FROM completions WHERE report_id = ? AND role = 'system' "
                           "ORDER BY created_at DESC LIMIT 1", (report_id,)).fetchone()
        assert comp is not None, "no system completion persisted"
        ex = con.execute("SELECT id FROM agent_executions WHERE completion_id = ? ORDER BY created_at DESC LIMIT 1",
                         (comp["id"],)).fetchone()
        tools = con.execute("SELECT tool_name, success FROM tool_executions WHERE agent_execution_id = ? "
                            "ORDER BY created_at", (ex["id"],)).fetchall() if ex else []
    finally:
        con.close()
    return {"completion": dict(comp), "tools": [dict(t) for t in tools]}


if __name__ == "__main__":
    sys.exit(main())
