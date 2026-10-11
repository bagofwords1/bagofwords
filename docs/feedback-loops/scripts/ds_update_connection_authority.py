"""Live repro: data-source manager rewriting a shared connection via PUT /api/data_sources/{id}.

Prereqs: tools/agent/boot_stack.sh running; seed_org.py run with --invite dsmgr@example.com;
echo MCP server on :3333 (tests/mocks/echo_mcp_http_server.py). Run from backend/:
    uv run python ../docs/feedback-loops/scripts/ds_update_connection_authority.py
"""
import pathlib, uuid, httpx

B = "http://localhost:8000/api"
CHINOOK = str((pathlib.Path(__file__).resolve().parents[3] / "backend/tests/config/chinook.sqlite"))
c = httpx.Client(timeout=60)

def login(email):
    r = c.post(f"{B}/auth/jwt/login", data={"username": email, "password": "Password123!"})
    r.raise_for_status(); return r.json()["access_token"]

at = login("admin@example.com")
org = c.get(f"{B}/users/whoami", headers={"Authorization": f"Bearer {at}"}).json()["organizations"][0]["id"]
H = lambda t: {"Authorization": f"Bearer {t}", "X-Organization-Id": org}
mt = login("dsmgr@example.com")
mid = c.get(f"{B}/users/whoami", headers=H(mt)).json()["id"]
tag = uuid.uuid4().hex[:6]

sql = c.post(f"{B}/connections", headers=H(at), json={"name": f"shared-sql-{tag}", "type": "sqlite",
             "config": {"database": CHINOOK}, "credentials": {}}).json()
mcp = c.post(f"{B}/connections", headers=H(at), json={"name": f"shared-mcp-{tag}", "type": "mcp",
             "config": {"server_url": "http://localhost:3333/mcp", "transport": "streamable_http"},
             "credentials": {}, "auth_policy": "system_only"}).json()
ds = c.post(f"{B}/data_sources", headers=H(at), json={"name": f"agent-{tag}", "connection_ids": [sql["id"], mcp["id"]],
            "generate_summary": False, "generate_conversation_starters": False, "generate_ai_rules": False}).json()
grant = lambda rt, rid, perms: c.post(f"{B}/organizations/{org}/resource-grants", headers=H(at), json={
    "resource_type": rt, "resource_id": rid, "principal_type": "user", "principal_id": mid, "permissions": perms}).status_code
print("grants:", grant("data_source", ds["id"], ["manage"]),
      grant("connection", sql["id"], ["create_data_sources"]), grant("connection", mcp["id"], ["create_data_sources"]))

def conn_state():
    return {k: (lambda j: (j["config"], j["auth_policy"]))(c.get(f"{B}/connections/{v['id']}", headers=H(at)).json())
            for k, v in (("sql", sql), ("mcp", mcp))}

before = conn_state()
for body in ({"config": {"database": "/tmp/attacker.sqlite"}}, {"credentials": {"password": "x"}},
             {"auth_policy": "user_required"}):
    r = c.put(f"{B}/data_sources/{ds['id']}", headers=H(mt), json=body)
    print(f"dsmgr PUT {list(body)[0]:12s} -> {r.status_code}")
after = conn_state()
print("connections unchanged:", before == after, "" if before == after else f"\n  before={before}\n  after ={after}")
print("dsmgr PUT name/description ->", c.put(f"{B}/data_sources/{ds['id']}", headers=H(mt),
      json={"name": f"renamed-{tag}", "description": "d"}).status_code)
for k, conn in (("sql", sql), ("mcp", mcp)):
    u = f"{B}/data_sources/{ds['id']}/connections/{conn['id']}"
    print(f"dsmgr unlink {k} ->", c.delete(u, headers=H(mt)).status_code,
          f"| relink {k} ->", c.post(u + "?sync_tables=false", headers=H(mt)).status_code)
print("admin PUT config ->", c.put(f"{B}/data_sources/{ds['id']}", headers=H(at),
      json={"config": {"database": CHINOOK}}).status_code)
