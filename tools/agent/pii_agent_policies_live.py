#!/usr/bin/env python3
"""Live permutation loop for per-agent PII policies (docs/feedback-loops/pii-agent-policies.md).

For each case: write the PII config through the settings API, open a report
with the case's agents, send one chat message through a real agent run
(AgentV2 → LLM → the capture mock), and check what EVERY LLM call of that run
sent to the provider (tools/agent/pii_capture_llm.py records each call).

The message carries an email and a phone number that are in no agent's data,
so whatever reaches the mock comes from the user's prompt:

    "Contact jane.roe@contoso.com or call 415-555-0134 about the Q3 numbers."

Prereqs: the stack (tools/agent/boot_stack.sh), the mock on :9199, and
tools/agent/pii_agent_policies_setup.py (agents HR and Sales, provider "Mock LLM").

Run (from backend/): uv run python ../tools/agent/pii_agent_policies_live.py [--db-path db/agent.db]
"""
import argparse
import json
import os
import sqlite3
import time

import httpx

p = argparse.ArgumentParser()
p.add_argument("--base-url", default=os.environ.get("BOW_BASE_URL", "http://localhost:8000"))
p.add_argument("--mock", default="http://127.0.0.1:9199")
p.add_argument("--db-path", default="db/agent.db")
p.add_argument("--out", default=None, help="write the results as JSON here")
a = p.parse_args()

EMAIL, PHONE = "jane.roe@contoso.com", "415-555-0134"
PROMPT = f"Contact {EMAIL} or call {PHONE} about the Q3 numbers."
OFF = {rid: {"enabled": False} for rid in ("email", "credit_card", "us_ssn", "phone", "ipv4", "iban", "aws_access_key")}
POLICIES = {
    "default": {"name": "Default", "mode": "replace", "builtin_overrides": OFF},
    "hr": {"name": "HR (emails + phones)", "mode": "replace",
           "builtin_overrides": {**OFF, "email": {"enabled": True}, "phone": {"enabled": True}}},
    "sales": {"name": "Sales (emails)", "mode": "replace",
              "builtin_overrides": {**OFF, "email": {"enabled": True}}},
    "hr_block": {"name": "HR (block emails)", "mode": "replace",
                 "builtin_overrides": {**OFF, "email": {"enabled": True, "action": "block"}}},
}

c = httpx.Client(base_url=a.base_url, timeout=120)
tok = c.post("/api/auth/jwt/login", data={
    "username": os.environ.get("BOW_EMAIL", "admin@example.com"),
    "password": os.environ.get("BOW_PASSWORD", "Password123!"),
}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}
agents = {d["name"]: d["id"] for d in c.get("/api/data_sources", params={"show_all": True}, headers=H).json()}
HR, SALES, MUSIC = agents["HR"], agents["Sales"], agents.get("Music Store")
mock_provider = next(pr["id"] for pr in c.get("/api/llm/providers", headers=H).json() if pr["name"] == "Mock LLM")


def configure(agent_policies=None, default_agent="none", provider_policies=None):
    r = c.put("/api/organization/settings", json={"config": {"pii_protection": {
        "enabled": True, "policies": POLICIES,
        "provider_policies": provider_policies or {},
        "agent_policies": agent_policies or {},
        "default_agent_policy": default_agent,
    }}}, headers=H)
    r.raise_for_status()


def run(data_sources):
    """One agent run in a fresh report. Returns (calls the mock received, final status, error text)."""
    c.delete(f"{a.mock}/captures")
    rep = c.post("/api/reports", json={"title": "PII agent loop", "widget": None, "files": [],
                                        "data_sources": data_sources}, headers=H).json()
    r = c.post(f"/api/reports/{rep['id']}/completions", params={"background": True}, headers=H,
               json={"prompt": {"content": PROMPT, "widget_id": None, "step_id": None, "mentions": [{}]}})
    r.raise_for_status()
    deadline = time.monotonic() + 240
    time.sleep(3)
    while time.monotonic() < deadline:
        with sqlite3.connect(a.db_path) as conn:
            n = conn.execute("SELECT COUNT(*) FROM completions WHERE report_id=? AND status='in_progress'",
                             (rep["id"],)).fetchone()[0]
        if n == 0:
            break
        time.sleep(2)
    time.sleep(2)  # late calls (titles, summaries) after the run ends
    # Completions are encrypted at rest: read the outcome through the API.
    comps = c.get(f"/api/reports/{rep['id']}/completions", headers=H).json().get("completions") or []
    system = [x for x in comps if x.get("role") == "system"]
    last = system[-1] if system else {}
    body = last.get("completion") or {}
    err = body.get("error") or {}
    text = err.get("code", "") + " " + str(err.get("summary") or body.get("content") or "")
    return c.get(f"{a.mock}/captures").json(), last.get("status"), text[:300]


def seen(calls):
    """Per call, whether the email / phone reached the provider."""
    return [{"email": EMAIL in x["text"], "phone": PHONE in x["text"]} for x in calls if PROMPT[:7] in x["text"] or "Q3" in x["text"]]


RAW, EMAIL_HIDDEN, BOTH_HIDDEN, BLOCKED = "raw", "email hidden, phone raw", "both hidden", "blocked"


def outcome(calls, status, err):
    s = seen(calls)
    if not s:
        return BLOCKED if "pii_blocked" in err else f"no calls ({status}: {err})"
    if all(x["email"] and x["phone"] for x in s):
        return RAW
    if all(not x["email"] and not x["phone"] for x in s):
        return BOTH_HIDDEN
    if all(not x["email"] and x["phone"] for x in s):
        return EMAIL_HIDDEN
    return "MIXED " + json.dumps(s)


CASES = [
    # (name, config kwargs, agents in the report, expected)
    ("HR-pii + Sales-none, chat with both", dict(agent_policies={HR: "hr"}), [HR, SALES], BOTH_HIDDEN),
    ("HR-pii + Sales-none, chat with Sales only", dict(agent_policies={HR: "hr"}), [SALES], RAW),
    ("HR-pii + Sales-none, chat with HR only", dict(agent_policies={HR: "hr"}), [HR], BOTH_HIDDEN),
    ("HR-pii, chat with no agents (Auto: every accessible agent)", dict(agent_policies={HR: "hr"}), [], BOTH_HIDDEN),
    ("HR-pii + Sales-emails, chat with Sales", dict(agent_policies={HR: "hr", SALES: "sales"}), [SALES], EMAIL_HIDDEN),
    ("HR-pii + Sales-emails, chat with both (union)", dict(agent_policies={HR: "hr", SALES: "sales"}), [HR, SALES], BOTH_HIDDEN),
    ("default agent = Sales-emails, Sales inherits", dict(default_agent="sales"), [SALES], EMAIL_HIDDEN),
    ("default agent = Sales-emails, Sales explicit none", dict(default_agent="sales", agent_policies={SALES: "none"}), [SALES], RAW),
    ("default agent = Sales-emails, HR own policy wins for HR", dict(default_agent="sales", agent_policies={HR: "hr"}), [HR], BOTH_HIDDEN),
    ("provider Exempt + HR-pii, chat with HR", dict(agent_policies={HR: "hr"}, provider_policies={mock_provider: "exempt"}), [HR], BOTH_HIDDEN),
    ("provider Exempt + HR-pii, chat with Sales", dict(agent_policies={HR: "hr"}, provider_policies={mock_provider: "exempt"}), [SALES], RAW),
    ("provider Sales-emails + HR-none-assigned, chat with HR", dict(provider_policies={mock_provider: "sales"}), [HR], EMAIL_HIDDEN),
    ("HR blocks emails + Sales, chat with both", dict(agent_policies={HR: "hr_block"}), [HR, SALES], BLOCKED),
    ("HR blocks emails, chat with Sales only", dict(agent_policies={HR: "hr_block"}), [SALES], RAW),
    ("nothing configured, chat with both", dict(), [HR, SALES], RAW),
]
if MUSIC:
    CASES.append(("default agent = Sales-emails, Music Store inherits", dict(default_agent="sales"), [MUSIC], EMAIL_HIDDEN))

results = []
for name, cfg, ds, expected in CASES:
    configure(**cfg)
    calls, status, err = run(ds)
    got = outcome(calls, status, err)
    ok = got == expected
    results.append({"case": name, "expected": expected, "got": got, "calls": len(calls), "pass": ok})
    print(f"{'PASS' if ok else 'FAIL'}  {name}: expected {expected}, got {got} ({len(calls)} LLM calls)", flush=True)

configure()  # leave nothing assigned
passed = sum(r["pass"] for r in results)
print(f"\n{passed}/{len(results)} live cases passed")
if a.out:
    with open(a.out, "w") as f:
        json.dump(results, f, indent=2)
raise SystemExit(0 if passed == len(results) else 1)
