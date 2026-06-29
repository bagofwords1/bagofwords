"""Prove a service account can be added to an agent (data source) and run a real query.

Flow (admin sets up; the SERVICE ACCOUNT runs the query via its API key):
  1. Configure Anthropic (haiku) LLM provider + model (default).
  2. Install the 'chinook' demo data source (an "agent").
  3. Create a service account 'Query Bot' (member role) + issue an API key.
  4. Grant the service account access to the data source (DataSourceMembership).
  5. As the service account: create a report bound to the data source, then run
     a completion ("How many artists...?") and print the agent's answer.

Env: BOW_DATABASE_URL must match the running server. ANTHROPIC_API_KEY required.
"""
import os, sys, time, sqlite3, json
import httpx

BASE = "http://localhost:8000"
ADMIN = {"username": "admin@example.com", "password": "supersecret123"}
HAIKU = "claude-haiku-4-5-20251001"
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
DB = os.environ.get("SA_DB_PATH", "db/app.db")


def main():
    c = httpx.Client(base_url=BASE, timeout=120)
    jwt = c.post("/api/auth/jwt/login", data=ADMIN).json()["access_token"]
    h = {"Authorization": f"Bearer {jwt}"}
    org = c.get("/api/organizations", headers=h).json()[0]["id"]
    H = {**h, "X-Organization-Id": org}
    print("org:", org)

    # 1. Anthropic provider + haiku model (default + small default)
    prov = c.post("/api/llm/providers", headers=H, json={
        "name": "Anthropic", "provider_type": "anthropic", "credentials": {"api_key": ANTHROPIC_KEY},
    })
    print("provider:", prov.status_code)
    prov_id = prov.json().get("id") if prov.status_code in (200, 201) else None
    if prov_id:
        m = c.post("/api/llm/models", headers=H, json={
            "name": "Claude Haiku", "model_id": HAIKU, "provider_id": prov_id,
            "is_default": True, "is_small_default": True,
        })
        print("model:", m.status_code, m.text[:200])

    # 2. Install chinook demo data source
    inst = c.post("/api/data_sources/demos/chinook", headers=H)
    print("demo install:", inst.status_code, inst.text[:200])
    ds_id = None
    try:
        j = inst.json()
        ds_id = j.get("data_source_id") or j.get("id") or (j.get("data_source") or {}).get("id")
    except Exception:
        pass
    if not ds_id:
        ds = c.get("/api/data_sources", headers=H).json()
        ds_id = (ds[0]["id"] if isinstance(ds, list) and ds else (ds.get("data_sources") or [{}])[0].get("id"))
    print("data_source id:", ds_id)

    # 3. Service account + key
    sa = c.post("/api/service_accounts", headers=H, json={"name": "Query Bot", "description": "Runs analytics queries"}).json()
    sa_id = sa["id"]
    key = c.post(f"/api/service_accounts/{sa_id}/keys", headers=H, json={"name": "run"}).json()["key"]
    print("service account:", sa_id, "key:", key[:14] + "…")

    # backing user id (for the data-source grant) — from the DB
    con = sqlite3.connect(DB)
    backing_user_id = con.execute("SELECT user_id FROM service_accounts WHERE id=?", (sa_id,)).fetchone()[0]
    con.close()
    print("backing user:", backing_user_id)

    # 4. Grant the SA access to the data source (added to the agent)
    g = c.post(f"/api/data_sources/{ds_id}/members", headers=H,
               json={"principal_type": "user", "principal_id": backing_user_id})
    print("grant DS access:", g.status_code, g.text[:160])

    # 5. As the SERVICE ACCOUNT: create a report + run a query
    sa_h = {"X-API-Key": key}
    rep = c.post("/api/reports", headers=sa_h, json={"title": "SA analytics", "data_sources": [ds_id]})
    print("SA create report:", rep.status_code, rep.text[:160])
    report_id = rep.json()["id"]

    prompt = "How many rows are in the artists table? Answer with the number."
    comp = c.post(f"/api/reports/{report_id}/completions", headers=sa_h,
                  json={"prompt": {"content": prompt}, "stream": False})
    print("SA completion:", comp.status_code)
    out = comp.text
    print("---- completion response (truncated) ----")
    print(out[:1500])

    # poll the report's completions for the agent's final answer
    for _ in range(30):
        comps = c.get(f"/api/reports/{report_id}/completions", headers=sa_h)
        if comps.status_code == 200:
            data = comps.json()
            items = data if isinstance(data, list) else data.get("completions", [])
            ai = [x for x in items if x.get("role") in ("system", "ai_agent", "assistant") or x.get("message_type") == "ai_completion"]
            done = [x for x in ai if x.get("status") in ("success", "error")]
            if done:
                print("\n==== FINAL AGENT MESSAGES ====")
                for x in done[-3:]:
                    body = x.get("completion") or {}
                    content = body.get("content") if isinstance(body, dict) else body
                    print(f"[{x.get('status')}] {str(content)[:600]}")
                break
        time.sleep(4)
    print("\nDONE — report_id:", report_id)
    # write a marker file for the screenshot step
    with open("/tmp/.sa_report", "w") as f:
        f.write(report_id)


if __name__ == "__main__":
    main()
