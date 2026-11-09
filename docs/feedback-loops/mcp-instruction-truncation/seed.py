"""Seed the live backend through its real API: admin, MCP on, API key, and
three published instructions of very different sizes. Writes state.json."""
import json, pathlib, subprocess, sys, uuid, httpx

ROOT = pathlib.Path(__file__).resolve().parents[3]

BASE = "http://localhost:8000"
seed = json.loads(subprocess.check_output(
    [sys.executable, str(ROOT / "tools/agent/seed_org.py"), "--base-url", BASE,
     "--db-path", str(ROOT / "backend/db/loop.db")], text=True))
token, org = seed["admin"]["token"], seed["organization"]["id"]
h = {"Authorization": f"Bearer {token}", "X-Organization-Id": org}
c = httpx.Client(base_url=BASE, headers=h, timeout=60)
assert c.put("/api/organization/settings", json={"config": {"mcp_enabled": {"value": True}}}).status_code == 200
key = c.post("/api/api_keys", json={"name": "loop"}).json()["key"]

tag = uuid.uuid4().hex[:6]
def body(n):
    out, i = [], 0
    while sum(len(x) + 1 for x in out) < n:
        out.append(f"## Section {i}" if i % 7 == 0 else f"- rule {i}: revenue is booked net of refunds")
        i += 1
    return "\n".join(out)

medium = f"[{tag}] Revenue rules\n" + body(3_000) + "\nIMPORTANT LAST RULE: revenue questions use fct_orders, never stg_orders."
long = f"[{tag}] Event catalogue\n" + body(160_000) + "\nLAST LINE OF THE CATALOGUE"
giant = f"[{tag}] Mapping blob\n" + "{" + '"k": 1, ' * 15_000 + "} TAIL use fct_orders\nend"
ids = {}
for name, text in (("medium", medium), ("long", long), ("giant", giant)):
    r = c.post("/api/instructions/global", json={"text": text, "status": "published", "category": "general", "data_source_ids": []})
    assert r.status_code == 200, r.text
    ids[name] = {"id": r.json()["id"], "chars": len(text), "lines": text.count("\n") + 1, "text": text}
json.dump({"key": key, "tag": tag, "ids": ids}, open(pathlib.Path(__file__).with_name("state.json"), "w"))
print("seeded", {k: (v["chars"], v["lines"]) for k, v in ids.items()})
