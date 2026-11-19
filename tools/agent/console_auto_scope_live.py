#!/usr/bin/env python3
"""Live loop for agent managers monitoring Auto runs (docs/feedback-loops/console-auto-scope.md).

Drives the REAL API and agent loop (only the LLM is stubbed — tools/agent/stub_llm.py):

  1. three private agents (sqlite_source_1..3 from seed_org.py --sqlite-sources 3)
  2. Max  — member with `manage` on sources 1 and 2
     Mia  — member who can use sources 1 and 2 only
  3. Mia asks an Auto question (no agent picked)          -> run exposed to {1, 2}
  4. admin grants Mia source 3; Mia asks again in the same report -> run exposed to {1, 2, 3}
  5. prints what Max sees on every console surface

Phases (the stub must be restarted between them with a different source list):
    uv run python ../tools/agent/console_auto_scope_live.py setup
    uv run python ../tools/agent/console_auto_scope_live.py turn1
    uv run python ../tools/agent/console_auto_scope_live.py turn2
    uv run python ../tools/agent/console_auto_scope_live.py inspect
State is kept in $BOW_AGENT_RUN_DIR/console_auto_scope.json (default /tmp/bow-agent).
"""
import json
import os
import pathlib
import sqlite3
import sys
import time

import httpx

BASE = os.environ.get("BOW_BASE_URL", "http://localhost:8000")
ADMIN = (os.environ.get("BOW_ADMIN_EMAIL", "admin@example.com"), os.environ.get("BOW_ADMIN_PASSWORD", "Password123!"))
PASSWORD = "Password123!"
STATE = pathlib.Path(os.environ.get("BOW_AGENT_RUN_DIR", "/tmp/bow-agent")) / "console_auto_scope.json"
DB = os.environ.get("BOW_SQLITE_DB", str(pathlib.Path(__file__).resolve().parents[2] / "backend" / "db" / "agent.db"))

c = httpx.Client(base_url=BASE, timeout=180, trust_env=False)


def login(email, password):
    r = c.post("/api/auth/jwt/login", data={"username": email, "password": password})
    r.raise_for_status()
    return r.json()["access_token"]


def H(tok, org):
    return {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}


def load():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2))


def user(org, admin_h, email, name):
    r = c.post(f"/api/organizations/{org}/members", json={"organization_id": org, "email": email, "role": "member"},
               headers=admin_h)
    assert r.status_code in (200, 400), r.text
    # Accept the invitation the way the emailed link does (token from the membership row).
    con = sqlite3.connect(DB)
    row = con.execute("select invite_token from memberships where email=? and user_id is null "
                      "order by created_at desc limit 1", (email,)).fetchone()
    con.close()
    body = {"email": email, "password": PASSWORD, "name": name}
    if row:
        body["invite_token"] = row[0]
    r = c.post("/api/auth/register", json=body)
    assert r.status_code in (200, 201, 400), r.text
    tok = login(email, PASSWORD)
    return {"email": email, "id": c.get("/api/users/whoami", headers={"Authorization": f"Bearer {tok}"}).json()["id"]}


def grant(org, admin_h, ds_id, user_id, perms):
    r = c.post(f"/api/organizations/{org}/resource-grants", json={
        "resource_type": "data_source", "resource_id": ds_id,
        "principal_type": "user", "principal_id": user_id, "permissions": perms}, headers=admin_h)
    assert r.status_code in (200, 201) or "already exists" in r.text, r.text


def setup():
    tok = login(*ADMIN)
    org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
    ah = H(tok, org)
    agents = {d["name"]: d["id"] for d in c.get("/api/data_sources", headers=ah).json()}
    src = {i: agents[f"sqlite_source_{i}"] for i in (1, 2, 3)}
    for ds in agents.values():  # nothing reachable without a grant
        assert c.put(f"/api/data_sources/{ds}", json={"is_public": False}, headers=ah).status_code == 200
    max_ = user(org, ah, "max.manager@example.com", "Max Manager")
    mia = user(org, ah, "mia.member@example.com", "Mia Member")
    for i in (1, 2):
        grant(org, ah, src[i], max_["id"], ["manage"])
        grant(org, ah, src[i], mia["id"], ["view"])
    save({"org": org, "src": src, "max": max_, "mia": mia, "agents": agents})
    print("setup ok", json.dumps({"org": org, "src": src}, indent=1))


def ask(prompt):
    s = load()
    tok = login(s["mia"]["email"], PASSWORD)
    h = H(tok, s["org"])
    if not s.get("report"):
        r = c.post("/api/reports", json={"title": "Weekly orders check", "files": [], "data_sources": []}, headers=h)
        r.raise_for_status()
        s["report"] = r.json()["id"]
        save(s)
    r = c.post(f"/api/reports/{s['report']}/completions",
               json={"prompt": {"content": prompt, "mentions": []}, "stream": False}, headers=h, timeout=600)
    print("completion", r.status_code, r.text[:200])
    # Wait for the run to finish.
    for _ in range(120):
        con = sqlite3.connect(DB)
        busy = con.execute("select count(*) from agent_executions where report_id=? and status='in_progress'",
                           (s["report"],)).fetchone()[0]
        con.close()
        if not busy:
            break
        time.sleep(1)
    exposure()


def exposure():
    s = load()
    names = {v: k for k, v in s["agents"].items()}
    con = sqlite3.connect(DB)
    for ae, created, status in con.execute(
            "select id, created_at, status from agent_executions where report_id=? order by created_at", (s["report"],)):
        exp = [names.get(a, a) for (a,) in con.execute(
            "select agent_id from execution_exposed_agents where execution_id=? order by agent_id", (ae,))]
        usd = [names.get(a, a) for (a,) in con.execute(
            "select agent_id from folder_execution_agents where execution_id=? order by agent_id", (ae,))]
        print(f"run {ae[:8]} {created} {status:10} exposed={exp} used={usd}")
    con.close()


def turn1():
    ask("How many orders do we have, by region?")


def turn2():
    s = load()
    tok = login(*ADMIN)
    grant(s["org"], H(tok, s["org"]), s["src"]["3"], s["mia"]["id"], ["view"])
    ask("Now break the orders down by region for the third source too.")


def inspect():
    s = load()
    exposure()
    for who in ("max", "mia"):
        tok = login(s[who]["email"], PASSWORD)
        h = H(tok, s["org"])
        r = c.get("/api/console/agent_executions/summaries", headers=h)
        print(f"\n[{who}] summaries", r.status_code,
              [i["prompt"] for i in r.json().get("items", [])] if r.status_code == 200 else r.text[:120])
        r = c.get(f"/api/console/reports/{s['report']}/conversation", headers=h)
        if r.status_code == 200:
            b = r.json()
            print(f"[{who}] conversation", [t["user_prompt"] for t in b["turns"]], "hidden_turns", b["hidden_turns"])
        else:
            print(f"[{who}] conversation", r.status_code)


if __name__ == "__main__":
    {"setup": setup, "turn1": turn1, "turn2": turn2, "inspect": inspect, "exposure": exposure}[sys.argv[1]]()
