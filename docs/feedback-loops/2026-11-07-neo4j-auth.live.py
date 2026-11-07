"""Loop B for docs/feedback-loops/2026-11-07-neo4j-auth.md: the Neo4j connector's
auth options through the running app, with a real LLM.

Needs: the stack (tools/agent/boot_stack.sh + seed_org.py), the Neo4j
containers and seed from the doc, and ANTHROPIC_KEY in the environment.

For each connection it checks Test connection, the indexed catalog, and one
question answered by the agent (create_data must succeed and the reply must
carry the expected values).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

import httpx

BASE = os.environ.get("BOW_BASE_URL", "http://localhost:8000")
EMAIL, PASSWORD = os.environ.get("BOW_EMAIL", "admin@example.com"), os.environ.get("BOW_PASSWORD", "Password123!")
MODEL = os.environ.get("BOW_LIVE_MODEL", "claude-haiku-5-5")
NEO4J = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_NOAUTH = os.environ.get("NEO4J_NOAUTH_URI", "bolt://localhost:7689")
CATALOG = {"(:Person)", "(:Movie)", "[:KNOWS]", "[:ACTED_IN]"}

c = httpx.Client(base_url=BASE, timeout=120)
results: list[dict] = []


def ok(r, *codes):
    if r.status_code not in (codes or (200, 201)):
        sys.exit(f"{r.request.method} {r.request.url} -> {r.status_code} {r.text[:800]}")
    return r.json() if r.content else None


token = ok(c.post("/api/auth/jwt/login", data={"username": EMAIL, "password": PASSWORD}))["access_token"]
me = ok(c.get("/api/users/whoami", headers={"Authorization": f"Bearer {token}"}))
org = me["organizations"][0]["id"]
H = {"Authorization": f"Bearer {token}", "X-Organization-Id": org}

if not any(p.get("provider_type") == "anthropic" for p in ok(c.get("/api/llm/providers", headers=H))):
    key = os.environ.get("ANTHROPIC_KEY") or sys.exit("ANTHROPIC_KEY not set")
    ok(c.post("/api/llm/providers", headers=H, json={
        "name": "anthropic-live", "provider_type": "anthropic", "credentials": {"api_key": key},
        "models": [{"model_id": MODEL, "name": MODEL, "is_custom": False, "is_default": True,
                    "is_small_default": True}],
    }))


def test_params(config, credentials):
    return ok(c.post("/api/data_sources/test_connection", headers=H, json={
        "name": "probe", "type": "neo4j", "config": config, "credentials": credentials}))


def create(name, config, credentials, **extra):
    ds = ok(c.post("/api/data_sources", headers=H, json={
        "name": f"{name}_{int(time.time()) % 100000}", "type": "neo4j", "config": config,
        "credentials": credentials, "auth_policy": "system_only", "generate_summary": False,
        "generate_conversation_starters": False, "generate_ai_rules": False, **extra}))
    names: set[str] = set()
    for _ in range(90):
        body = ok(c.get(f"/api/data_sources/{ds['id']}/full_schema", headers=H))
        items = body.get("tables", body) if isinstance(body, dict) else body
        names = {t.get("name") for t in items}
        if names >= CATALOG:
            break
        time.sleep(1)
    ok(c.put(f"/api/data_sources/{ds['id']}/update_tables_status", headers=H,
             json={"activate": sorted(names), "deactivate": []}))
    return ds, names


def completions(report_id):
    rows = ok(c.get(f"/api/reports/{report_id}/completions", headers=H, params={"limit": 200}))
    return rows.get("completions", rows) if isinstance(rows, dict) else rows


def ask(ds, question):
    report = ok(c.post("/api/reports", headers=H, json={
        "title": f"neo4j live {ds['name']}", "files": [], "data_sources": [ds["id"]]}))
    ok(c.post(f"/api/reports/{report['id']}/completions", headers=H, timeout=900, json={
        "prompt": {"content": question, "mentions": [], "mode": "chat"}, "stream": False}), 200, 201, 202)
    deadline = time.time() + 600
    while time.time() < deadline:
        comps = completions(report["id"])
        if comps and not any((x.get("status") or "") in ("in_progress", "pending", "queued", "running") for x in comps):
            break
        time.sleep(3)
    text = json.dumps(comps)
    tools = re.findall(r'"tool_name":\s*"([a-z_]+)"', text)
    statuses = re.findall(r'"tool_name":\s*"create_data".{0,400}?"status":\s*"([a-z_]+)"', text)
    replies = []
    for comp in comps:
        if comp.get("role") == "system":
            for b in comp.get("completion_blocks") or []:
                content = (b.get("content") if isinstance(b, dict) else None) or ""
                if content.strip():
                    replies.append(content.strip())
    return {"report_id": report["id"], "tools": tools, "create_data": statuses, "reply": "\n".join(replies)[-1500:]}


def record(name, **data):
    results.append({"case": name, **data})
    print(f"\n=== {name}\n{json.dumps(data, indent=2, default=str)[:3000]}", flush=True)


# 1. Username / password: catalog, a Cypher question, a two-hop question.
config = {"uri": NEO4J, "database": "neo4j", "auth_type": "userpass"}
creds = {"username": "neo4j", "password": "test-password-123"}
probe = test_params(config, creds)
ds, names = create("neo4j_userpass", config, creds)
answer = ask(ds, "Who are the 3 people with the highest salary, and how many movies has each acted in? "
                 "Give name, salary and movie count.")
record("userpass", test=probe, catalog=sorted(names), **answer)

# 2. Service user + impersonation (system): queries run as ann, whose role cannot read salary.
config = {"uri": NEO4J, "database": "neo4j", "auth_type": "impersonation"}
creds = {"username": "svc", "password": "svc-password-123", "impersonated_user": "ann"}
probe = test_params(config, creds)
ds, names = create("neo4j_impersonation", config, creds)
answer = ask(ds, "What is the salary of P30? If it is not available, say so plainly.")
record("impersonation_as_ann", test=probe, catalog=sorted(names), **answer)

# 3. Impersonation per member: Require user authentication, the admin's own
#    membership mapped to ann via a member attribute (member_mapped).
members = ok(c.get(f"/api/organizations/{org}/members", headers=H))
mine = next(m for m in members if (m.get("user") or {}).get("email") == EMAIL or m.get("email") == EMAIL)
ok(c.put(f"/api/organizations/{org}/members/{mine['id']}", headers=H,
         json={"custom_attributes": {"neo4j_user": "ann"}}))
creds = {"username": "svc", "password": "svc-password-123"}
ds, names = create("neo4j_member_mapped", config, creds, auth_policy="user_required",
                   member_bindings={"impersonated_user": "neo4j_user"})
conn_id = (ds.get("connections") or [{}])[0].get("id") or ds.get("connection_id")
member_test = ok(c.post(f"/api/connections/{conn_id}/test-my-credentials", headers=H)) if conn_id else None
record("impersonation_member_mapped", connection_id=conn_id, my_test=member_test, catalog=sorted(names))

# 4. Graph algorithms allowed by the admin.
config = {"uri": NEO4J, "database": "neo4j", "auth_type": "userpass", "allow_gds": True}
creds = {"username": "neo4j", "password": "test-password-123"}
ds, names = create("neo4j_gds", config, creds)
answer = ask(ds, "Run PageRank over the KNOWS network and list the 5 most influential people with their scores.")
record("gds_pagerank", catalog=sorted(names), **answer)

# 5. Row limit set by the admin.
config = {"uri": NEO4J, "database": "neo4j", "auth_type": "userpass", "max_rows": 5}
ds, names = create("neo4j_rowcap", config, creds)
answer = ask(ds, "List every person's name, one row each (all of them).")
record("row_limit_5", **answer)

# 6. No authentication (a server with auth disabled) and a bearer token on a
#    server without SSO (must fail cleanly, token never shown).
record("no_auth", test=test_params({"uri": NEO4J_NOAUTH, "database": "neo4j", "auth_type": "none"}, {}))
bearer = "eyJhbGciOiJSUzI1NiJ9.e30.live-test-token"
probe = test_params({"uri": NEO4J, "database": "neo4j", "auth_type": "bearer"}, {"token": bearer})
record("bearer_without_sso", test=probe, token_hidden=bearer not in json.dumps(probe))

out = os.environ.get("LOOP_OUT")
if out:
    with open(out, "w") as f:
        json.dump(results, f, indent=2, default=str)
