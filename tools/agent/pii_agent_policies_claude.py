#!/usr/bin/env python3
"""Live leg on a real model for per-agent PII policies
(docs/feedback-loops/pii-agent-policies.md).

The model really queries the agents' tables, so the PII here comes from the
DATA (query results), not from the prompt. The backend must run with
BOW_LLM_TRACE_FILE (+ BOW_LLM_TRACE_FULL=1): the trace records each request as
sent to the provider, after PII redaction.

Policies: HR → "HR (emails + phones)"; Sales → none; default agent policy none;
the Anthropic provider on Default (no rules). Cases:

1. chat with HR only, asking for employees' emails and phones
   → no HR email / phone in any request to Anthropic;
2. chat with Sales only, asking for account contact emails
   → Sales emails reach Anthropic (no policy applies: the control);
3. chat with HR + Sales, asking for Sales contact emails
   → Sales emails do NOT reach Anthropic (HR's policy covers the whole chat).

Prereqs: pii_agent_policies_setup.py run with ANTHROPIC_KEY_LOOP set.
Run (from backend/): uv run python ../tools/agent/pii_agent_policies_claude.py --trace /tmp/bow-agent/llm_trace.jsonl
"""
import argparse
import json
import os
import re
import time

import httpx

p = argparse.ArgumentParser()
p.add_argument("--base-url", default=os.environ.get("BOW_BASE_URL", "http://localhost:8000"))
p.add_argument("--trace", required=True)
p.add_argument("--model", default="claude-haiku-4-5")
p.add_argument("--out", default=None)
a = p.parse_args()

HR_EMAILS = ["dana.levi@acme-hr.example", "omar.haddad@acme-hr.example", "rina.cohen@acme-hr.example"]
HR_PHONES = ["415-555-0101", "415-555-0102", "415-555-0103"]
SALES_EMAILS = ["buyer@globex.example", "ap@initech.example", "procurement@umbrella.example"]
OFF = {rid: {"enabled": False} for rid in ("email", "credit_card", "us_ssn", "phone", "ipv4", "iban", "aws_access_key")}

c = httpx.Client(base_url=a.base_url, timeout=120)
tok = c.post("/api/auth/jwt/login", data={
    "username": os.environ.get("BOW_EMAIL", "admin@example.com"),
    "password": os.environ.get("BOW_PASSWORD", "Password123!"),
}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}
agents = {d["name"]: d["id"] for d in c.get("/api/data_sources", params={"show_all": True}, headers=H).json()}
HR, SALES = agents["HR"], agents["Sales"]
model = next(m for m in c.get("/api/llm/models", headers=H).json() if m["model_id"] == a.model)

r = c.put("/api/organization/settings", json={"config": {"pii_protection": {
    "enabled": True,
    "policies": {
        "default": {"name": "Default", "mode": "replace", "builtin_overrides": OFF},
        "hr": {"name": "HR (emails + phones)", "mode": "replace",
               "builtin_overrides": {**OFF, "email": {"enabled": True}, "phone": {"enabled": True}}},
        "sales": {"name": "Sales (emails)", "mode": "replace",
                  "builtin_overrides": {**OFF, "email": {"enabled": True}}},
    },
    "provider_policies": {},
    "agent_policies": {HR: "hr"},
    "default_agent_policy": "none",
}}}, headers=H)
r.raise_for_status()


def trace_lines():
    try:
        with open(a.trace) as f:
            return f.readlines()
    except FileNotFoundError:
        return []


def run(data_sources, prompt):
    start = len(trace_lines())
    rep = c.post("/api/reports", json={"title": "PII agent loop (Claude)", "widget": None, "files": [],
                                        "data_sources": data_sources}, headers=H).json()
    c.post(f"/api/reports/{rep['id']}/completions", params={"background": True}, headers=H, json={
        "prompt": {"content": prompt, "widget_id": None, "step_id": None, "mentions": [{}], "model_id": model["id"]},
    }).raise_for_status()
    deadline = time.monotonic() + 300
    status, answer = None, ""
    while time.monotonic() < deadline:
        time.sleep(4)
        comps = c.get(f"/api/reports/{rep['id']}/completions", headers=H).json().get("completions") or []
        system = [x for x in comps if x.get("role") == "system"]
        if system and system[-1].get("status") not in ("in_progress", None):
            status = system[-1]["status"]
            answer = str((system[-1].get("completion") or {}).get("content") or "")
            break
    time.sleep(3)
    sent = "".join(trace_lines()[start:])
    calls = len(trace_lines()) - start
    return {"report": rep["id"], "status": status, "calls": calls, "sent": sent, "answer": answer[:600]}


def leaked(sent, values):
    return [v for v in values if v in sent]


results = []


def check(name, ok, **detail):
    results.append({"check": name, "pass": bool(ok), **detail})
    print(("PASS " if ok else "FAIL ") + name, json.dumps(detail, default=str)[:700], flush=True)


r1 = run([HR], "List every employee with their email address and phone number.")
check("HR chat: the run reached the model", r1["calls"] > 0, status=r1["status"], calls=r1["calls"])
check("HR chat: no HR email or phone reached Anthropic", not leaked(r1["sent"], HR_EMAILS + HR_PHONES),
      leaked=leaked(r1["sent"], HR_EMAILS + HR_PHONES))
check("HR chat: the data reached the model redacted", "[REDACTED_EMAIL]" in r1["sent"] and "[REDACTED_PHONE]" in r1["sent"],
      email_tokens=r1["sent"].count("[REDACTED_EMAIL]"), phone_tokens=r1["sent"].count("[REDACTED_PHONE]"))
check("HR chat: names (not covered by the policy) did reach the model", "Dana Levi" in r1["sent"])

r2 = run([SALES], "List every account with its contact email.")
check("Sales chat (control): Sales emails reached Anthropic as written", len(leaked(r2["sent"], SALES_EMAILS)) >= 1,
      seen=leaked(r2["sent"], SALES_EMAILS), calls=r2["calls"], status=r2["status"])

r3 = run([HR, SALES], "Using the Sales agent, list every account with its contact email.")
check("HR + Sales chat: Sales emails did NOT reach Anthropic", not leaked(r3["sent"], SALES_EMAILS),
      leaked=leaked(r3["sent"], SALES_EMAILS), calls=r3["calls"], status=r3["status"])
check("HR + Sales chat: the Sales data reached the model redacted", "[REDACTED_EMAIL]" in r3["sent"],
      email_tokens=r3["sent"].count("[REDACTED_EMAIL]"))
check("no context markers in any request", not re.search("[-]", r1["sent"] + r2["sent"] + r3["sent"]))

for name, r in (("HR", r1), ("Sales", r2), ("HR+Sales", r3)):
    print(f"\n--- {name} answer ({r['status']}, {r['calls']} calls) ---\n{r['answer']}")

passed = sum(x["pass"] for x in results)
print(f"\n{passed}/{len(results)} checks passed")
if a.out:
    with open(a.out, "w") as f:
        json.dump({"checks": results, "answers": {k: v["answer"] for k, v in (("hr", r1), ("sales", r2), ("both", r3))}},
                  f, indent=2)
raise SystemExit(0 if passed == len(results) else 1)
