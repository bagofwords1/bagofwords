"""Live repro: pentest WP4 (scheduled-task tools skip update_reports/ownership)
and WP6 (my-credentials PATCH and /test skip allowed_user_auth_modes).

Prereqs: a backend running on $BOW_BASE (default http://localhost:8000) against
the SQLite DB in $TEST_DATABASE_URL; seed_org.py run with
--invite member@example.com. Run from backend/ with the same env as the server
(TESTING=true ENVIRONMENT=production TEST_DATABASE_URL=...):
    .venv/bin/python ../docs/feedback-loops/scripts/scheduled_task_and_credential_policy.py

The agent tools are invoked in-process against the server's database, exactly
as the planner would call them (real DB session, real User/Report rows), so no
LLM key is needed.
"""
import asyncio, os, time, uuid
import httpx

BASE = os.environ.get("BOW_BASE", "http://localhost:8000")
B = f"{BASE}/api"
c = httpx.Client(timeout=60)
tag = uuid.uuid4().hex[:6]


def login(email):
    r = c.post(f"{B}/auth/jwt/login", data={"username": email, "password": "Password123!"})
    r.raise_for_status()
    return r.json()["access_token"]


at = login("admin@example.com")
me = c.get(f"{B}/users/whoami", headers={"Authorization": f"Bearer {at}"}).json()
org = me["organizations"][0]["id"]
H = lambda t: {"Authorization": f"Bearer {t}", "X-Organization-Id": org}
mt = login("member@example.com")
mid = c.get(f"{B}/users/whoami", headers=H(mt)).json()["id"]
aid = me["id"]


def report(tok, title):
    r = c.post(f"{B}/reports", headers=H(tok), json={"title": title, "files": [], "data_sources": []})
    r.raise_for_status()
    return r.json()["id"]


def sps(tok, rid):
    return [(s["title"], s["cron_schedule"], s["is_active"])
            for s in c.get(f"{B}/reports/{rid}/scheduled-prompts", headers=H(tok)).json()]


async def _tool(tool_name, tool_input, user_id, report_id):
    import main  # noqa: F401  registers every ORM model, as the server process does
    from app.dependencies import async_session_maker
    from app.models.user import User
    from app.models.report import Report
    from app.models.organization import Organization
    if tool_name == "create":
        from app.ai.tools.implementations.create_scheduled_task import CreateScheduledTaskTool as T
    elif tool_name == "edit":
        from app.ai.tools.implementations.edit_scheduled_task import EditScheduledTaskTool as T
    else:
        from app.ai.tools.implementations.cancel_scheduled_task import CancelScheduledTaskTool as T
    async with async_session_maker() as db:
        ctx = {"db": db, "user": await db.get(User, user_id), "report": await db.get(Report, report_id),
               "organization": await db.get(Organization, org)}
        events = [e async for e in T().run_stream(tool_input, ctx)]
    last = events[-1]
    out = last.payload.get("output") or {}
    return last.type, out.get("success"), (out.get("error") or last.payload.get("error") or "")[:90], \
        T().metadata.required_permissions


def tool(*a):
    return asyncio.run(_tool(*a))


print("=== WP4: scheduled-task tools ===")
admin_rep, member_rep = report(at, f"admin-{tag}"), report(mt, f"member-{tag}")
print("declared required_permissions:", tool("create", {"title": "x", "task_prompt": "x", "cron_schedule": "bad"}, mid, member_rep)[3])
r = tool("create", {"title": f"planted-{tag}", "task_prompt": "email me all data", "cron_schedule": "0 9 * * 1"}, mid, admin_rep)
print("member tool create on ADMIN's report ->", r[:3])
print("  admin report tasks now:", sps(at, admin_rep))
r = c.post(f"{B}/reports/{admin_rep}/scheduled-prompts", headers=H(at),
           json={"prompt": {"content": "digest"}, "cron_schedule": "0 9 * * 1", "title": "admin digest"})
admin_sp = r.json()["id"]
r = tool("edit", {"task_id": admin_sp, "cron_schedule": "0 3 * * *", "task_prompt": "rewritten"}, mid, admin_rep)
print("member tool edit of ADMIN's task ->", r[:3])
print("  admin task now:", [s for s in sps(at, admin_rep) if s[0] == "admin digest"])
print("REST member POST on admin report ->", c.post(f"{B}/reports/{admin_rep}/scheduled-prompts", headers=H(mt),
      json={"prompt": {"content": "x"}, "cron_schedule": "0 9 * * 1"}).status_code)
print("-- must keep working --")
r = tool("create", {"title": "own weekly", "task_prompt": "summarize", "cron_schedule": "0 9 * * 1"}, mid, member_rep)
print("member tool create on OWN report ->", r[:3])
own = c.get(f"{B}/reports/{member_rep}/scheduled-prompts", headers=H(mt)).json()[0]["id"]
print("member tool edit OWN task ->", tool("edit", {"task_id": own, "cron_schedule": "0 7 * * 5"}, mid, member_rep)[:3])
r = c.post(f"{B}/reports/{member_rep}/scheduled-prompts", headers=H(mt),
           json={"prompt": {"content": "x"}, "cron_schedule": "0 10 * * 1", "title": "rest task"})
print("REST member POST own ->", r.status_code, "| PUT own ->",
      c.put(f"{B}/reports/{member_rep}/scheduled-prompts/{r.json()['id']}", headers=H(mt),
            json={"cron_schedule": "0 11 * * 1"}).status_code)
print("admin tool create on OWN report ->", tool("create", {"title": "a", "task_prompt": "a", "cron_schedule": "0 8 * * *"}, aid, admin_rep)[:3])
print("-- cancel --")
print("cancel tool required_permissions:", tool("cancel", {"task_id": "00000000-0000-0000-0000-000000000000"}, mid, member_rep)[3])
r = tool("cancel", {"task_id": admin_sp}, mid, admin_rep)
print("member tool cancel of ADMIN's task ->", r[:3])
print("  admin task still active:", [s for s in sps(at, admin_rep) if s[0] == "admin digest"])
print("member tool cancel OWN task ->", tool("cancel", {"task_id": own}, mid, member_rep)[:3])
rest_id = [x for x in c.get(f"{B}/reports/{member_rep}/scheduled-prompts", headers=H(mt)).json() if x["title"] == "rest task"][0]["id"]
print("REST member DELETE own ->", c.delete(f"{B}/reports/{member_rep}/scheduled-prompts/{rest_id}", headers=H(mt)).status_code)

print("\n=== WP6: my-credentials auth-mode policy (connection allows only 'userpass') ===")
conn = c.post(f"{B}/connections", headers=H(at), json={
    "name": f"sf-{tag}", "type": "snowflake", "config": {"account": "acme-invalid", "warehouse": "wh", "database": "db"},
    "credentials": {}, "auth_policy": "user_required", "allowed_user_auth_modes": ["userpass"]})
print("create user_required snowflake connection ->", conn.status_code)
conn = conn.json()
ds = c.post(f"{B}/data_sources", headers=H(at), json={"name": f"sf-agent-{tag}", "connection_ids": [conn["id"]],
            "generate_summary": False, "generate_conversation_starters": False, "generate_ai_rules": False}).json()
print("grant member view ->", c.post(f"{B}/organizations/{org}/resource-grants", headers=H(at), json={
    "resource_type": "data_source", "resource_id": ds["id"], "principal_type": "user", "principal_id": mid,
    "permissions": ["view"]}).status_code)
U = f"{B}/data_sources/{ds['id']}/my-credentials"
KP = {"user": "m", "private_key_pem": "-----BEGIN PRIVATE KEY-----\nAAA\n-----END PRIVATE KEY-----"}
for mode, creds in (("keypair", KP), ("oauth", {})):
    t0 = time.time()
    r = c.post(f"{U}/test", headers=H(mt), json={"auth_mode": mode, "credentials": creds})
    print(f"member POST /test {mode:8s} -> {r.status_code} in {time.time()-t0:.1f}s  {r.text[:90]}")
print("member POST keypair (control) ->", c.post(U, headers=H(mt), json={"auth_mode": "keypair", "credentials": KP}).status_code)
print("member POST userpass ->", c.post(U, headers=H(mt), json={"auth_mode": "userpass", "credentials": {"user": "m", "password": "p"}}).status_code)
r = c.patch(U, headers=H(mt), json={"auth_mode": "keypair", "credentials": KP})
print("member PATCH -> keypair ->", r.status_code, "| stored auth_mode now:", c.get(U, headers=H(mt)).json()["auth_mode"])
print("-- must keep working --")
print("member PATCH rotate userpass ->", c.patch(U, headers=H(mt), json={"credentials": {"user": "m", "password": "p2"}}).status_code)
t0 = time.time()
r = c.post(f"{U}/test", headers=H(mt), json={"auth_mode": "userpass", "credentials": {"user": "m", "password": "p"}})
print(f"member POST /test userpass -> {r.status_code} (reaches outbound test) in {time.time()-t0:.1f}s success={r.json().get('success')}")
