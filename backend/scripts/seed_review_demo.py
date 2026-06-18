"""Seed the Review feed for full local testing.

Builds on seed_access_demo.py (agents + suggestions must already exist). Adds:
  1. An Anthropic **Haiku** LLM model for the org — only if an API key is in env
     (BOW_DEMO_ANTHROPIC_KEY or ANTHROPIC_API_KEY). Never hardcode a key.
  2. A "Demo Evals" suite + active eval cases scoped to two agents, so the
     Review actions (run eval / run training) actually have something to run.
  3. Deterministic quality signals (DB-level): slow query tool-executions
     (>90s) and low-confidence completions (response_score < 3).
  4. Runs /api/review/scan so the feed is populated.

Usage:
  cd backend && BOW_SEED_BASE=http://localhost:8000 \
      BOW_DEMO_ANTHROPIC_KEY=sk-ant-... \
      python scripts/seed_review_demo.py
"""
import os
import sys
import json
import uuid
import sqlite3
import urllib.request
import urllib.error
from datetime import datetime

BASE = os.environ.get("BOW_SEED_BASE", "http://localhost:8000")
ADMIN_EMAIL = os.environ.get("BOW_SEED_ADMIN", "sandbox@bow.dev")
ADMIN_PASSWORD = os.environ.get("BOW_SEED_ADMIN_PW", "Sandbox123!")
ANTHROPIC_KEY = os.environ.get("BOW_DEMO_ANTHROPIC_KEY") or os.environ.get("ANTHROPIC_API_KEY")
SLOW_AGENT = os.environ.get("BOW_DEMO_SLOW_AGENT", "Public Catalog")
LOWCONF_AGENT = os.environ.get("BOW_DEMO_LOWCONF_AGENT", "Public Sales")

_token = None
_org = None


def req(method, path, body=None, form=False, token=None, org=True):
    url = BASE + path
    headers = {}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if org and _org:
        headers["X-Organization-Id"] = _org
    if body is not None:
        if form:
            data = "&".join(f"{k}={v}" for k, v in body.items()).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode()[:300]}


def setup_admin():
    global _token, _org
    tok = req("POST", "/api/auth/jwt/login",
              {"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, form=True, org=False)
    _token = tok.get("access_token")
    if not _token:
        print("FATAL: admin login failed", tok); sys.exit(1)
    me = req("GET", "/api/auth/me", token=_token, org=False)
    orgs = req("GET", "/api/organizations", token=_token, org=False)
    _org = (orgs[0]["id"] if isinstance(orgs, list) and orgs else me.get("organization_id"))
    print(f"admin ok, org={_org}")


def agents_by_name():
    ds = req("GET", "/api/data_sources", token=_token)
    items = ds if isinstance(ds, list) else ds.get("items", [])
    return {a["name"]: a["id"] for a in items}


def ensure_llm():
    if not ANTHROPIC_KEY:
        print("• no API key in env — skipping LLM model (run_eval will report no_evals)")
        return
    existing = req("GET", "/api/llm/providers", token=_token)
    if isinstance(existing, list) and any(p.get("provider_type") == "anthropic" for p in existing):
        print("• Anthropic provider already present")
        return
    res = req("POST", "/api/llm/providers", {
        "name": "Anthropic",
        "provider_type": "anthropic",
        "credentials": {"api_key": ANTHROPIC_KEY},
        "models": [{
            "name": "Claude 4.5 Haiku",
            "model_id": "claude-haiku-4-5-20251001",
            "is_default": True,
            "is_small_default": True,
            "supports_vision": True,
            "context_window_tokens": 200000,
        }],
    }, token=_token)
    print("• created Anthropic Haiku provider:", "ok" if not res.get("_error") else res)


def ensure_evals(agents):
    suite = req("POST", "/api/tests/suites", {"name": "Demo Evals", "description": "Seeded eval cases"}, token=_token)
    sid = suite.get("id")
    if not sid:
        print("! could not create suite:", suite); return
    cases = [
        (SLOW_AGENT, "Revenue by year", "Show total revenue by year", "revenue"),
        (LOWCONF_AGENT, "Top customers", "Who are our top 5 customers by spend?", "customer"),
    ]
    made = 0
    for agent_name, case_name, prompt, expect in cases:
        aid = agents.get(agent_name)
        if not aid:
            continue
        res = req("POST", f"/api/tests/suites/{sid}/cases", {
            "name": case_name,
            "prompt_json": {"content": prompt},
            "expectations_json": {"rules": [{
                "type": "field",
                "target": {"category": "completion", "field": "text"},
                "matcher": {"type": "text.contains", "value": expect},
            }]},
            "data_source_ids_json": [aid],
            "status": "active",
        }, token=_token)
        cid = res.get("id")
        if cid and res.get("status") != "active":
            req("PATCH", f"/api/tests/cases/{cid}", {"status": "active"}, token=_token)
        made += 1 if cid else 0
    print(f"• created {made} active eval case(s) in 'Demo Evals'")


def _db_path():
    url = os.environ.get("BOW_DATABASE_URL", "sqlite:///db/app.db")
    p = url.replace("sqlite+aiosqlite:///", "").replace("sqlite:///", "")
    return p if os.path.isabs(p) else os.path.join(os.getcwd(), p)


def inject_signals(agents):
    """DB-level: slow query tool-executions + low-confidence completions.

    Faithful to the real detectors (ToolExecution.duration_ms / response_score);
    there is no HTTP API to fabricate historical agent runs."""
    path = _db_path()
    if not os.path.exists(path):
        print("! db not found for signal injection:", path); return
    c = sqlite3.connect(path); cur = c.cursor()
    now = datetime.utcnow().isoformat()
    uid = cur.execute("select id from users where email=?", (ADMIN_EMAIL,)).fetchone()[0]

    def report(ds_id, title):
        rid = str(uuid.uuid4())
        cur.execute("""insert into reports(id,created_at,updated_at,title,slug,status,user_id,organization_id,report_type,conversation_share_enabled,mode,artifact_visibility,conversation_visibility)
          values(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (rid, now, now, title, 'r-'+rid[:8], 'active', uid, _org, 'standard', 0, 'default', 'private', 'private'))
        cur.execute("insert into report_data_source_association(report_id,data_source_id) values(?,?)", (rid, ds_id))
        return rid

    def completion(rid, score):
        cid = str(uuid.uuid4())
        cur.execute("""insert into completions(id,created_at,updated_at,prompt,completion,status,model,turn_index,message_type,role,report_id,main_router,feedback_score,response_score)
          values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (cid, now, now, json.dumps({"content": "q"}), json.dumps({"content": "a"}), 'success', 'claude-haiku', 0, 'ai_completion', 'assistant', rid, 'default', 0, score))
        return cid

    def slow_tool(rid, ms):
        cid = completion(rid, 4)
        aid = str(uuid.uuid4())
        cur.execute("""insert into agent_executions(id,created_at,updated_at,completion_id,status,latest_seq,is_eval_run,report_id)
          values(?,?,?,?,?,?,?,?)""", (aid, now, now, cid, 'success', 1, 0, rid))
        cur.execute("""insert into tool_executions(id,created_at,updated_at,agent_execution_id,tool_name,arguments_json,status,success,attempt_number,max_retries,duration_ms)
          values(?,?,?,?,?,?,?,?,?,?,?)""", (str(uuid.uuid4()), now, now, aid, 'create_data', json.dumps({}), 'success', 1, 1, 0, ms))

    slow = agents.get(SLOW_AGENT); low = agents.get(LOWCONF_AGENT)
    if slow:
        r = report(slow, 'Catalog perf run')
        for _ in range(5):
            slow_tool(r, 125000)
        r2 = report(slow, 'Catalog answers')
        for _ in range(3):
            completion(r2, 2)
    if low:
        r3 = report(low, 'Sales answers')
        for _ in range(4):
            completion(r3, 2)
    c.commit(); c.close()
    print(f"• injected slow queries on '{SLOW_AGENT}' and low-confidence runs on '{SLOW_AGENT}'/'{LOWCONF_AGENT}'")


def main():
    setup_admin()
    agents = agents_by_name()
    if not agents:
        print("FATAL: no agents — run seed_access_demo.py first"); sys.exit(1)
    ensure_llm()
    ensure_evals(agents)
    inject_signals(agents)
    res = req("POST", "/api/review/scan", token=_token)
    print("• review scan:", res)
    print("done.")


if __name__ == "__main__":
    main()
