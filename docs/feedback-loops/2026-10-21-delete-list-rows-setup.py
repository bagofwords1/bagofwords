"""Loop B setup for delete_list_rows (see 2026-10-21-delete-list-rows-chat-tool.md).

Against a running stack (tools/agent/boot_stack.sh + seed_org.py --demo):
  1. registers an OpenAI-compatible provider at STUB_BASE_URL (the scripted stub
     or any real gateway) and makes its model the default + small default;
  2. creates a keyed list "Vendors" on the Music Store agent and fills 6 rows
     (rows go through the same submission service as submit_list — there is no
     REST insert endpoint, so this step writes in-process to the stack's DB);
  3. creates a chat report on the agent and starts a run asking to delete rows.

Prints the report id for the UI driver (frontend/tests/lists/delete-list-rows-flow.mjs).

Run from backend/ with the same env the stack uses:
  BOW_ENCRYPTION_KEY=... TEST_DATABASE_URL=sqlite:///db/agent.db TESTING=true \
  uv run python ../docs/feedback-loops/2026-10-21-delete-list-rows-setup.py [--prompt "..."] [--model stub]
"""
import argparse
import asyncio
import os
import uuid

import httpx

BASE = os.environ.get("BOW_BACKEND", "http://localhost:8000")
VENDORS = [("Acme Corp", 1200), ("Globex", 860), ("Initech", 430), ("Umbrella", 2100),
           ("Hooli", 990), ("Stark Industries", 3100)]

ap = argparse.ArgumentParser()
ap.add_argument("--prompt", default="Please delete Acme Corp and Initech from the Vendors list.")
ap.add_argument("--model", default=os.environ.get("STUB_MODEL", "stub"))
ap.add_argument("--base-url", default=os.environ.get("STUB_BASE_URL", "http://127.0.0.1:9098/v1"))
ap.add_argument("--skip-provider", action="store_true")
args = ap.parse_args()

c = httpx.Client(base_url=BASE, timeout=60)
tok = c.post("/api/auth/jwt/login", data={"username": "admin@example.com", "password": "Password123!"}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}

# 1. LLM provider
if not args.skip_provider:
    provs = c.get("/api/llm/providers", headers=H).json()
    prov = next((p for p in provs if p["name"] == "loop-provider"), None)
    if prov is None:
        r = c.post("/api/llm/providers", headers=H, json={
            "name": "loop-provider", "provider_type": "custom",
            "credentials": {"base_url": args.base_url, "api_key": os.environ.get("STUB_API_KEY", "stub")},
            "models": [{"model_id": args.model, "name": args.model, "is_custom": True}]})
        assert r.status_code == 200, r.text
        prov = r.json()
    model = next(m for m in prov["models"] if m["model_id"] == args.model)
    for small in ("false", "true"):
        r = c.post(f"/api/llm/models/{model['id']}/set_default?small={small}", headers=H)
        assert r.status_code == 200, r.text

# 2. List + rows
ds = next(d for d in c.get("/api/data_sources", headers=H).json() if d["name"] == "Music Store")
lists = c.get(f"/api/data_sources/{ds['id']}/lists", headers=H).json()
lst = next((l for l in lists if l["name"] == "Vendors"), None)
if lst is None:
    r = c.post(f"/api/data_sources/{ds['id']}/lists", headers=H, json={
        "name": "Vendors", "description": "One row per vendor", "key_field": "vendor",
        "fields": [{"name": "vendor", "type": "string", "required": True, "description": "Vendor name"},
                   {"name": "annual_spend", "type": "number", "unit": "USD", "description": "Spend per year"}]})
    assert r.status_code == 201, r.text
    lst = r.json()


async def seed_rows():
    from app.dependencies import async_session_maker
    from app.models.agent_list import AgentList
    from app.services.agent_lists.records import apply_submission_with_retry

    env = lambda v: {"value": v, "status": "found", "evidence": [], "note": None}  # noqa: E731
    async with async_session_maker() as db:
        row = await db.get(AgentList, lst["id"])
        res = await apply_submission_with_retry(
            db, row, [{"row_id": None, "fields": {"vendor": env(n), "annual_spend": env(v)}} for n, v in VENDORS],
            report_id=None, tool_execution_id=None, user_id=None, sources=[])
        print("seeded rows:", res["inserted"], "inserted,", res["updated"], "updated,", res["unchanged"], "unchanged")

asyncio.run(seed_rows())
total = c.get(f"/api/data_sources/{ds['id']}/lists/{lst['id']}/rows", headers=H).json()["total"]
print("list", lst["id"], "rows now:", total)

# 3. Report + run (chat mode — the background API's default)
rep = c.post("/api/reports", headers=H, json={"title": f"Delete rows {uuid.uuid4().hex[:4]}", "data_sources": [ds["id"]]}).json()
r = c.post(f"/api/reports/{rep['id']}/completions?background=true", headers=H,
           json={"prompt": {"content": args.prompt, "mentions": [{}]}})
assert r.status_code == 200, r.text
print("REPORT_ID", rep["id"])
print("LIST", ds["id"], lst["id"])
