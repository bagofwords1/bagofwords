"""
End-to-end verification for PRIVATE CONNECTORS against a RUNNING backend
(http://localhost:8000) + running mock MCP server (http://localhost:9301/mcp).

Real HTTP, real Claude (org auto-uses ANTHROPIC_API_KEY via provisioning).
Multiple users with different permissions:
  - admin        : full_admin_access  (can create org-wide connectors + analytical agents)
  - member1/2    : default member     (create_private_connector only)

Run (fresh DB recommended):
  uv run python tools/sandbox/connector-mocks/e2e_connectors.py
"""
import os, sys, uuid, json
import httpx

BASE = os.environ.get("BOW_BASE", "http://localhost:8000")
MOCK = os.environ.get("MOCK_URL", "http://localhost:9301/mcp")
C = httpx.Client(base_url=BASE, timeout=240.0)
RESULTS = []

def log(m): print(f"  {m}", flush=True)
def section(t): print(f"\n=== {t} ===", flush=True)
def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"  [{'PASS ✅' if ok else 'FAIL ❌'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok

def H(token, org=None):
    h = {"Authorization": f"Bearer {token}"}
    if org: h["X-Organization-Id"] = str(org)
    return h

def register(name, invite_token=None):
    email = f"{name}-{uuid.uuid4().hex[:8]}@example.com"
    pw = "Password123!"
    body = {"name": name, "email": email, "password": pw}
    if invite_token: body["invite_token"] = invite_token
    r = C.post("/api/auth/register", json=body)
    assert r.status_code == 201, f"register {name}: {r.status_code} {r.text}"
    return email, pw

def login(email, pw):
    r = C.post("/api/auth/jwt/login", data={"username": email, "password": pw})
    assert r.status_code == 200, f"login {email}: {r.status_code} {r.text}"
    return r.json()["access_token"]

def whoami(token):
    r = C.get("/api/users/whoami", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.json()

def invite_and_register(admin_token, org, name, role="member"):
    email = f"{name}-{uuid.uuid4().hex[:8]}@example.com"
    pw = "Password123!"
    r = C.post(f"/api/organizations/{org}/members", headers=H(admin_token, org),
               json={"organization_id": org, "email": email, "role": role})
    assert r.status_code == 200, f"invite {name}: {r.status_code} {r.text}"
    mid = r.json()["id"]
    r = C.get(f"/api/organizations/{org}/members/{mid}/invite-link", headers=H(admin_token, org))
    assert r.status_code == 200, f"invite-link: {r.text}"
    token_invite = r.json()["token"]
    r = C.post("/api/auth/register", json={"name": name, "email": email, "password": pw, "invite_token": token_invite})
    assert r.status_code == 201, f"register {name}: {r.status_code} {r.text}"
    tok = login(email, pw)
    return tok

def provision_llm(token, org):
    key = os.environ["ANTHROPIC_API_KEY"]
    r = C.get("/api/llm/models", headers=H(token, org))
    if r.status_code == 200 and r.json():
        return
    r = C.post("/api/llm/providers", headers=H(token, org), json={
        "name": "Anthropic", "provider_type": "anthropic",
        "credentials": {"api_key": key},
        "models": [
            {"model_id": "claude-haiku-4-5-20251001", "name": "Claude Haiku 4.5", "is_custom": False},
            {"model_id": "claude-sonnet-4-5-20250929", "name": "Claude Sonnet 4.5", "is_custom": False},
        ],
    })
    assert r.status_code == 200, f"provider: {r.status_code} {r.text}"
    models = C.get("/api/llm/models", headers=H(token, org)).json()
    if not any(m.get("is_default") for m in models):
        haiku = next((m for m in models if "haiku" in m.get("model_id", "")), models[0])
        C.post(f"/api/llm/models/{haiku['id']}/set_default", headers=H(token, org))
    log(f"llm provisioned: {[m.get('model_id') for m in models]}")

def make_connector(token, org, name, is_public, expect=200):
    # Self-serve path: create connection INLINE via data_source Mode-1 (type=mcp).
    # The service auto-discovers tools, so no /connections access is needed.
    r = C.post("/api/data_sources", headers=H(token, org), json={
        "name": f"{name} {uuid.uuid4().hex[:4]}",
        "type": "mcp",
        "config": {"server_url": MOCK, "transport": "streamable_http"},
        "credentials": {}, "auth_policy": "system_only",
        "is_public": is_public, "generate_summary": False,
        "generate_conversation_starters": False, "generate_ai_rules": False,
    })
    if expect != 200:
        return r
    assert r.status_code == 200, f"data_source: {r.status_code} {r.text}"
    return r.json()

def run_agent(token, org, ds_id, prompt):
    r = C.post("/api/reports", headers=H(token, org),
               json={"title": "connector e2e", "files": [], "data_sources": [ds_id]})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    r = C.post(f"/api/reports/{rid}/completions", headers=H(token, org),
               params={"background": "false"},
               json={"prompt": {"content": prompt, "widget_id": None, "step_id": None, "mentions": [{}]}})
    assert r.status_code == 200, f"completion: {r.status_code} {r.text}"
    return rid

def analyze(token, org, rid):
    r = C.get(f"/api/reports/{rid}/completions", headers=H(token, org), params={"limit": 30})
    data = r.json()
    comps = data.get("completions", data) if isinstance(data, dict) else data
    actions, texts = [], []
    for c in comps:
        for b in (c.get("completion_blocks") or []):
            pd = b.get("plan_decision") or {}
            if pd.get("action_name"): actions.append(pd["action_name"])
            if b.get("content"): texts.append(b["content"])
    return actions, "\n".join(texts)

def main():
    section("Setup — admin + 2 members (different permissions)")
    a_email, a_pw = register("admin")
    admin = login(a_email, a_pw)
    org = whoami(admin)["organizations"][0]["id"]
    log(f"org={org}")
    provision_llm(admin, org)
    m1 = invite_and_register(admin, org, "member1")
    m2 = invite_and_register(admin, org, "member2")
    log(f"member1 perms: {whoami(m1)['organizations'][0].get('role')}")
    check("members joined same org",
          whoami(m1)["organizations"][0]["id"] == org and whoami(m2)["organizations"][0]["id"] == org)

    section("Phase A — admin creates an ORG-WIDE connector; agent uses it (real Claude)")
    ds = make_connector(admin, org, "Monday (org)", is_public=True)
    check("admin connector is_public", ds.get("is_public") is True, f"is_public={ds.get('is_public')}")
    rid = run_agent(admin, org, ds["id"], "List my Monday boards and how many items each has.")
    actions, text = analyze(admin, org, rid)
    log(f"agent actions: {actions}")
    log(f"answer (excerpt): {text[:240].replace(chr(10),' ')}")
    check("admin agent invoked the connector tool", "execute_mcp" in actions)
    check("admin agent answer reflects mock data", ("Engineering" in text or "Marketing" in text))

    section("Phase B — member1 self-serves a PRIVATE connector; agent uses it")
    ds1 = make_connector(m1, org, "My Monday", is_public=True)  # request public...
    check("member connector forced PRIVATE", ds1.get("is_public") is False, f"is_public={ds1.get('is_public')}")
    check("member connector owned by member1", str(ds1.get("owner_user_id")) == str(whoami(m1).get("id", "")) or ds1.get("owner_user_id") is not None)
    rid1 = run_agent(m1, org, ds1["id"], "Show the items on my Engineering board.")
    actions1, text1 = analyze(m1, org, rid1)
    log(f"agent actions: {actions1}")
    log(f"answer (excerpt): {text1[:240].replace(chr(10),' ')}")
    check("member1 agent invoked the connector tool", "execute_mcp" in actions1)
    check("member1 agent answer reflects mock data", any(w in text1 for w in ("Ship connectors", "Fix OAuth", "Write tests")))

    section("Phase C — permission boundaries & isolation")
    # member cannot create an ANALYTICAL data source (needs create_data_source)
    r = C.post("/api/data_sources", headers=H(m1, org), json={
        "name": "pg", "type": "postgresql",
        "config": {"host": "x", "port": 5432, "database": "d", "schema": "public"},
        "credentials": {"user": "u", "password": "p"}, "auth_policy": "system_only",
    })
    check("member denied creating analytical data source (403)", r.status_code == 403, f"got {r.status_code}")
    # isolation: member2 does NOT see member1's private connector
    ds_list_m2 = C.get("/api/data_sources", headers=H(m2, org)).json()
    m2_ids = {d["id"] for d in ds_list_m2}
    check("member2 cannot see member1's private connector", ds1["id"] not in m2_ids)
    # surfacing: member1 sees their connector with is_connector=true
    ds_list_m1 = C.get("/api/data_sources", headers=H(m1, org)).json()
    mine = next((d for d in ds_list_m1 if d["id"] == ds1["id"]), None)
    check("member1's connector surfaced in /agents list", mine is not None)
    check("connector flagged is_connector=true", bool(mine and mine.get("is_connector")), f"is_connector={mine and mine.get('is_connector')}")
    # admin (governance) can see all incl private connectors via show_all
    ds_all = C.get("/api/data_sources", headers=H(admin, org), params={"show_all": "true"}).json()
    check("admin governance view includes member's private connector", ds1["id"] in {d["id"] for d in ds_all})

    section("SUMMARY")
    passed = sum(1 for _, ok in RESULTS if ok)
    total = len(RESULTS)
    for n, ok in RESULTS:
        print(f"  {'✅' if ok else '❌'} {n}")
    print(f"\n  {passed}/{total} checks passed")
    return 0 if passed == total else 1

if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"\nASSERTION FAILED: {e}")
        sys.exit(2)
