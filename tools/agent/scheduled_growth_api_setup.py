#!/usr/bin/env python3
"""Register scheduled_growth_fake_api.py as a custom API agent, through the
real API: enable MCP tools, create the custom_api connection + its data source,
refresh the tool catalog. Prints {"data_source_id", "connection_id", "tool"}.

    cd backend && uv run python ../tools/agent/scheduled_growth_api_setup.py \
        --api-url http://127.0.0.1:9199
"""
import argparse
import json
import os
import time
import uuid

import httpx

p = argparse.ArgumentParser()
p.add_argument("--base-url", default="http://localhost:8000")
p.add_argument("--email", default=os.environ.get("BOW_EMAIL", "admin@example.com"))
p.add_argument("--password", default=os.environ.get("BOW_PASSWORD", "Password123!"))
p.add_argument("--api-url", default="http://127.0.0.1:9199")
a = p.parse_args()

c = httpx.Client(base_url=a.base_url, timeout=120, trust_env=False)
tok = c.post("/api/auth/jwt/login", data={"username": a.email, "password": a.password}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}

c.put("/api/organization/settings", json={"config": {"mcp_enabled": {"value": True}}}, headers=H).raise_for_status()

slug = f"ordersapi{uuid.uuid4().hex[:4]}"
conn = c.post("/api/connections", json={
    "name": slug, "type": "custom_api", "auth_policy": "system_only", "credentials": {},
    "config": {"base_url": a.api_url, "endpoints": [{
        "name": "get_orders", "method": "GET", "path": "/orders",
        "description": "All orders for the daily revenue dashboard", "parameters": []}]},
}, headers=H)
conn.raise_for_status()
conn_id = conn.json()["id"]
ds = c.post("/api/data_sources", json={
    "name": f"{slug} agent", "connection_id": conn_id, "generate_summary": False,
    "generate_conversation_starters": False, "generate_ai_rules": False,
    "use_llm_sync": False, "is_public": True,
}, headers=H)
ds.raise_for_status()
time.sleep(3)  # creation starts a background discovery run; let it settle
c.post(f"/api/connections/{conn_id}/refresh-tools", headers=H).raise_for_status()
print(json.dumps({"data_source_id": ds.json()["id"], "connection_id": conn_id,
                  "tool": f"mcp__{slug}__get_orders"}))
