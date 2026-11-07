"""Live: seed via REST, then act as an MCP client (official SDK) on /api/mcp."""
import asyncio, json, subprocess, sys, uuid, httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

BASE = "http://localhost:8000"
import pathlib
ROOT = str(pathlib.Path(__file__).resolve().parents[3])
seed = json.loads(subprocess.check_output([sys.executable, f"{ROOT}/tools/agent/seed_org.py", "--base-url", BASE,
    "--email", f"live{uuid.uuid4().hex[:6]}@example.com", "--db-path", f"{ROOT}/backend/db/loop2.db"], text=True))
h = {"Authorization": f"Bearer {seed['admin']['token']}", "X-Organization-Id": seed["organization"]["id"]}
c = httpx.Client(base_url=BASE, headers=h, timeout=60)
assert c.put("/api/organization/settings", json={"config": {"mcp_enabled": {"value": True}}}).status_code == 200
key = c.post("/api/api_keys", json={"name": "live"}).json()["key"]
db = f"{ROOT}/backend/tests/config/chinook.sqlite"
ds = {n: c.post("/api/data_sources", json={"name": f"{n}_agent", "type": "sqlite", "config": {"database": db},
                                           "credentials": {}, "auth_policy": "system_only", "generate_summary": False,
                                           "generate_conversation_starters": False, "generate_ai_rules": False}).json()
      for n in ("sales", "hr")}
assert all("id" in d for d in ds.values()), ds
# A report on the sales agent only (hr is out of its scope), owned by this user.
rid = c.post("/api/reports", json={"title": "Live catalog", "widget": None, "files": [],
                                   "data_sources": [ds["sales"]["id"]]}).json()["id"]

def inst(text, **kw):
    agents = kw.pop("agents", [])
    body = {"text": text, "status": "published", "data_source_ids": [ds[a]["id"] for a in agents], **kw}
    r = c.post("/api/instructions" if agents else "/api/instructions/global", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]

ids = {
  "global_always": inst("Fiscal year starts in July.", load_mode="always", title="Fiscal year"),
  "sales_always": inst("Amounts are stored in cents; divide by 100.", load_mode="always", agents=["sales"], title="Cents"),
  "big_always": inst("## Event catalogue\n" + "- event: rule line for the catalogue\n" * 3000, load_mode="always", title="Event catalogue"),
  "sales_smart": inst("Churn means 90 days without an invoice.", load_mode="intelligent", agents=["sales"], title="Churn definition"),
  "hr_smart": inst("Headcount excludes contractors.", load_mode="intelligent", agents=["hr"], title="Headcount rule"),
  "skill": inst("1. Profile the table. 2. Check nulls.", load_mode="intelligent", kind="skill", title="Profile a table"),
}
name = {v: k for k, v in ids.items()}

async def main():
    async with streamablehttp_client(f"{BASE}/api/mcp", headers={"X-API-Key": key}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            async def call(tool, args):
                res = await s.call_tool(tool, args); raw = res.content[0].text
                return json.loads(raw), len(raw)
            ctx, size = await call("get_context", {"report_id": rid})
            print(f"get_context: {size:,} chars, instructions_total={ctx['instructions_total']} truncated={ctx['instructions_truncated']}")
            for e in ctx["instructions"]:
                n = name.get(e["id"], "?other")
                print(f"  {n:14} kind={e['kind']:11} load_mode={e['load_mode']:11} required={e['required']!s:5} "
                      f"text={'%d chars' % len(e['text']) if e.get('text') is not None else '-':>12} tokens={e.get('tokens')}")
            print("  hr_smart listed:", ids["hr_smart"] in {e["id"] for e in ctx["instructions"]})
            print("  hint:", (ctx.get("instructions_hint") or "")[:110], "…")
            want = [e["id"] for e in ctx["instructions"] if e.get("text") is None]
            out, size = await call("read_instruction", {"instruction_ids": want, "report_id": rid})
            print(f"batch read {[name.get(i, 'built-in skill') for i in want]}: {size:,} chars -> read {[name.get(d['id'], 'built-in skill') for d in out['instructions']]} "
                  f"not_read={out.get('not_read_ids')} not_found={out.get('not_found_ids')}")
            print("  message:", out["message"])
            out, _ = await call("read_instruction", {"instruction_id": ids["hr_smart"], "report_id": rid})
            print("scoped read of hr_smart (other agent):", out["success"], out.get("error_message"))
            out, _ = await call("read_instruction", {"instruction_id": ids["hr_smart"]})
            print("unscoped read of hr_smart (admin visibility):", out["success"])
asyncio.run(main())
