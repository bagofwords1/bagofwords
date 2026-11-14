#!/usr/bin/env python3
"""Loop B for docs/feedback-loops/2026-11-14-decision-model-routing.md.

Live end-to-end check of decision-model routing against real providers,
through the real API of a running stack (tools/agent/boot_stack.sh +
seed_org.py --demo). Keys come from the environment only and are never
printed:

    OPENAI_API_KEY     OpenAI (chat models + gpt-6-luna Decisions)
    TYPESAFE_API_KEY   optional — adds TypeSafe Jev (System One) as a decision model

    cd backend && uv run python ../tools/agent/decision_routing_live.py --decision openai
    cd backend && uv run python ../tools/agent/decision_routing_live.py --decision jev

Steps: register providers → confirm the decision model is listed with
kind=decision and absent from the chat list → test_decision → route with the
decision method → run a simple and a hard prompt → print the routing step and
the usage rows from the database.
"""
import argparse
import json
import os
import pathlib
import sqlite3
import sys
import time

import httpx

p = argparse.ArgumentParser()
p.add_argument("--base-url", default=os.environ.get("BOW_BASE_URL", "http://localhost:8000"))
p.add_argument("--email", default="admin@example.com")
p.add_argument("--password", default="Password123!")
p.add_argument("--decision", choices=["openai", "jev"], default="openai")
p.add_argument("--small", default="gpt-6-luna", help="chat small model id")
p.add_argument("--strong", default="gpt-5.6-sol", help="stronger chat model id")
p.add_argument("--db-path", default=str(pathlib.Path(__file__).resolve().parents[2] / "backend/db/agent.db"))
a = p.parse_args()

openai_key = os.environ.get("OPENAI_API_KEY")
if not openai_key:
    sys.exit("OPENAI_API_KEY is not set.")
jev_key = os.environ.get("TYPESAFE_API_KEY")
if a.decision == "jev" and not jev_key:
    sys.exit("TYPESAFE_API_KEY is not set.")

c = httpx.Client(base_url=a.base_url, timeout=300, trust_env=False)
tok = c.post("/api/auth/jwt/login", data={"username": a.email, "password": a.password}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}


def ok(r, what):
    if r.status_code >= 400:
        sys.exit(f"{what} failed: {r.status_code} {r.text[:300]}")
    return r.json()


def step(msg):
    print(f"\n== {msg}")


# 1. Providers ---------------------------------------------------------------
step("providers")
providers = ok(c.get("/api/llm/providers", headers=H), "list providers")
if not any(pr["provider_type"] == "openai" for pr in providers):
    ok(c.post("/api/llm/providers", headers=H, json={
        "name": "OpenAI", "provider_type": "openai", "credentials": {"api_key": openai_key}, "models": [],
    }), "create openai provider")
    print("created OpenAI provider")
if jev_key and not any(pr["name"] == "TypeSafe" for pr in providers):
    ok(c.post("/api/llm/providers", headers=H, json={
        "name": "TypeSafe", "provider_type": "custom",
        "credentials": {"base_url": "https://api.typesafe.ai/v1", "api_key": jev_key},
        "models": [{"model_id": "jev-latest", "name": "TypeSafe Jev", "is_custom": True,
                    "model_kind": "decision", "config": {"decision_format": "systemone"},
                    "input_cost_per_million_tokens_usd": 0.042, "output_cost_per_million_tokens_usd": 0}],
    }), "create typesafe provider")
    print("created TypeSafe provider (System One)")

# 2. Model lists: decision models never in the chat list -----------------------
step("model lists")
chat = ok(c.get("/api/llm/models", headers=H), "chat list")
allm = ok(c.get("/api/llm/models", headers=H, params={"kind": "all"}), "all list")
decision = [m for m in allm if m["model_kind"] == "decision"]
print("decision models:", [(m["name"], m["decision"]["decision_format"], m["decision"]["api_model_id"]) for m in decision])
assert decision, "no decision model listed"
assert not any(m["model_kind"] == "decision" for m in chat), "a decision model leaked into the chat list"
print(f"chat list: {len(chat)} models, none of kind=decision ✓")

by_mid = {m["model_id"]: m for m in chat}
small, strong = by_mid.get(a.small), by_mid.get(a.strong)
if not small or not strong:
    sys.exit(f"chat models {a.small}/{a.strong} not found; available: {sorted(by_mid)}")
ok(c.post(f"/api/llm/models/{small['id']}/set_default", headers=H, params={"small": True}), "small default")
ok(c.post(f"/api/llm/models/{strong['id']}/set_default", headers=H, params={"small": False}), "default")
ok(c.post(f"/api/llm/models/{small['id']}/routing_hint", headers=H,
          json={"hint": "Simple lookups, greetings, one-number answers."}), "hint small")
ok(c.post(f"/api/llm/models/{strong['id']}/routing_hint", headers=H,
          json={"hint": "Multi-step analysis, joins across tables, dashboards and charts."}), "hint strong")

# 3. Test each decision model -----------------------------------------------
step("test_decision")
for m in decision:
    r = c.post(f"/api/llm/models/{m['id']}/test_decision", headers=H)
    print(m["name"], r.status_code, r.json())

chosen = next(m for m in decision if (m["model_id"] == "gpt-6-luna-decisions") == (a.decision == "openai"))
print("routing with:", chosen["name"])

# 4. Routing: decision method ------------------------------------------------
step("routing setting")
ok(c.put("/api/organization/settings", headers=H, json={"config": {
    "model_routing": {"value": True},
    "model_routing_decision": {"value": {"method": "decision", "decision_model_id": chosen["id"], "min_confidence": 0.6}},
}}), "routing settings")
print("model_routing on, method=decision")

# 5. Runs --------------------------------------------------------------------
ds = ok(c.get("/api/data_sources/active", headers=H), "data sources")
ds_ids = [ds[0]["id"]] if ds else []
prompts = [
    ("simple", "hi! what can you help me with?"),
    ("hard", "Build a dashboard comparing revenue by product category and month, with a chart of the top 5 "
             "categories and a table of month-over-month growth, joining orders with products."),
]
results = []
for label, prompt in prompts:
    step(f"run: {label}")
    report = ok(c.post("/api/reports", headers=H, json={"title": f"decision-{label}", "files": [], "data_sources": ds_ids}), "report")
    t0 = time.monotonic()
    r = c.post(f"/api/reports/{report['id']}/completions", headers=H, params={"background": False},
               json={"prompt": {"content": prompt, "widget_id": None, "step_id": None, "mentions": [{}]}})
    print("http", r.status_code, f"{time.monotonic() - t0:.1f}s")
    results.append((label, report["id"]))

# 6. Database evidence ---------------------------------------------------------
step("database")
time.sleep(3)
db = sqlite3.connect(a.db_path)
for label, rid in results:
    print(f"\n-- {label} (report {rid})")
    rows = db.execute(
        "select c.model, c.role from completions c where c.report_id=? order by c.created_at", (rid,)).fetchall()
    print("completions (model, role):", rows)
    te = db.execute(
        """select te.tool_name, te.tool_action, te.result_json from tool_executions te
           join agent_executions ae on ae.id = te.agent_execution_id
           where ae.report_id=? and te.tool_name='route_model'""", (rid,)).fetchall()
    for name, action, rj in te:
        j = json.loads(rj) if rj else {}
        print(f"route_model[{action}]: cause={j.get('cause')} model={j.get('model')} "
              f"confidence={j.get('confidence')} probs={j.get('probabilities')} by={j.get('decision_model')}")
    if not te:
        print("route_model steps: none")
    usage = db.execute(
        """select scope, model_id, routed, prompt_tokens, completion_tokens, round(total_cost_usd, 6)
           from llm_usage_records where report_id=? order by created_at""", (rid,)).fetchall()
    for u in usage:
        print("usage:", u)

step("cost console")
metrics = ok(c.get("/api/console/metrics/llm-usage", headers=H), "metrics")
print("routing:", json.dumps(metrics.get("routing"), indent=1))
