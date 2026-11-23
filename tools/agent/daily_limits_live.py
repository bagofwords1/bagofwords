#!/usr/bin/env python3
"""Live loop for daily + monthly usage caps against a running stack.

Drives real agent runs through the API and checks, for every metric (LLM
tokens, LLM spend, data queries, data scanned) and every window (daily,
monthly, both), that:

  1. a run records today's usage in the daily counters (org timezone), and
  2. once a cap is reached, the next run is blocked by that cap, with the
     daily/monthly wording the user sees.

The model is whatever the org default is. With a funded Anthropic key, run
setup_haiku_llm.py first and this exercises Claude Haiku; otherwise point it at
the OpenAI-compatible stub (stub_llm.py + setup_stub_llm.py). Cap enforcement
runs before the provider call either way.

    cd backend && uv run python ../tools/agent/daily_limits_live.py \
        --db-path db/agent.db --timezone Asia/Jerusalem
Prints a JSON report; exits non-zero when any check fails.
"""
import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone

import httpx

p = argparse.ArgumentParser()
p.add_argument("--base-url", default="http://localhost:8000")
p.add_argument("--email", default="admin@example.com")
p.add_argument("--password", default="Password123!")
p.add_argument("--db-path", default="db/agent.db")
p.add_argument("--timezone", default="Asia/Jerusalem")
p.add_argument("--prompt", default="Show total order amount by region from orders_1")
a = p.parse_args()

c = httpx.Client(base_url=a.base_url, timeout=120, trust_env=False)
tok = c.post("/api/auth/jwt/login", data={"username": a.email, "password": a.password}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}
me = c.get("/api/users/whoami", headers=H).json()
user_id = me["id"]

c.put("/api/organization/timezone", json={"timezone": a.timezone}, headers=H).raise_for_status()
sources = [s["id"] for s in c.get("/api/data_sources", headers=H).json() if s["name"].startswith("sqlite_source_")]

LIMIT_FIELDS = [f"{w}_{m}" for w in ("daily", "monthly")
                for m in ("token_limit", "query_limit", "data_bytes_limit", "spend_limit_usd")]
POLICY_NAME = "Daily limits live loop"


def set_policy(**limits):
    body = {k: limits.get(k) for k in LIMIT_FIELDS}
    existing = [x for x in c.get(f"/api/organizations/{org}/usage-policies", headers=H).json() if x["name"] == POLICY_NAME]
    if existing:
        r = c.put(f"/api/organizations/{org}/usage-policies/{existing[0]['id']}", json=body, headers=H)
    else:
        r = c.post(f"/api/organizations/{org}/usage-policies", json={
            "name": POLICY_NAME, **body,
            "assignments": [{"principal_type": "user", "principal_id": user_id}],
        }, headers=H)
    r.raise_for_status()
    return r.json()


def quota():
    q = next(o for o in c.get("/api/users/whoami", headers=H).json()["organizations"] if o["id"] == org)["usage_quota"]
    return q


def used_today():
    d = quota()["daily"]
    return {"tokens": d["tokens"]["used"], "queries": d["queries"]["used"],
            "data_bytes": d["data_bytes"]["used"], "spend": d["spend"]["used"]}


def used_month():
    q = quota()
    return {"tokens": q["tokens"]["used"], "queries": q["queries"]["used"],
            "data_bytes": q["data_bytes"]["used"], "spend": q["spend"]["used"]}


def run_prompt(label):
    """One agent run in a fresh report; returns what the user would see."""
    t0 = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")
    rep = c.post("/api/reports", json={"title": f"Live loop: {label}", "widget": None, "files": [],
                                        "data_sources": sources}, headers=H).json()
    r = c.post(f"/api/reports/{rep['id']}/completions", params={"background": True}, headers=H,
               json={"prompt": {"content": a.prompt, "widget_id": None, "step_id": None, "mentions": [{}]}})
    r.raise_for_status()
    deadline = time.monotonic() + 300
    time.sleep(3)
    while time.monotonic() < deadline:
        conn = sqlite3.connect(a.db_path)
        n_open = conn.execute("SELECT COUNT(*) FROM completions WHERE report_id=? AND status='in_progress'",
                              (rep["id"],)).fetchone()[0]
        conn.close()
        if n_open == 0:
            break
        time.sleep(2)
    conn = sqlite3.connect(a.db_path)
    rows = conn.execute("SELECT role, status, completion FROM completions WHERE report_id=? ORDER BY created_at",
                        (rep["id"],)).fetchall()
    tools = conn.execute(
        "SELECT te.tool_name, te.status, te.error_message, te.result_summary FROM tool_executions te "
        "WHERE te.created_at >= ? ORDER BY te.created_at", (t0,)).fetchall()
    conn.close()
    system = [r for r in rows if r[0] == "system"]
    text = json.loads(system[-1][2] or "{}") if system else {}
    shown = " ".join(str(text.get(k) or "") for k in ("content", "reasoning", "error")).strip()
    blob = json.dumps(text) + " " + " ".join(str(t) for t in tools)
    return {"report_id": rep["id"], "status": system[-1][1] if system else None,
            "message": shown[:400], "tools": [(t[0], t[1], (t[2] or t[3] or "")[:200]) for t in tools],
            "_blob": blob}


results = []


def check(name, ok, **detail):
    results.append({"check": name, "pass": bool(ok), **detail})
    print(("PASS " if ok else "FAIL ") + name, json.dumps({k: v for k, v in detail.items()}, default=str)[:600])


# ── 1. a run records today's usage, in every metric ───────────────────────
set_policy()  # assigned, nothing capped
before_day, before_month = used_today(), used_month()
run = run_prompt("baseline")
after_day, after_month = used_today(), used_month()
delta = {k: round(after_day[k] - before_day[k], 6) for k in after_day}
check("baseline run records today's usage in all four metrics",
      all(delta[k] > 0 for k in delta), delta_today=delta, run_status=run["status"])
check("daily and monthly counters move together",
      all(abs((after_month[k] - before_month[k]) - delta[k]) < 1e-6 for k in delta),
      delta_month={k: round(after_month[k] - before_month[k], 6) for k in delta})
daily = quota()["daily"]
check("daily window is today in the org timezone", daily["timezone"] == a.timezone,
      window=[daily["window_start"], daily["window_end"]], timezone=daily["timezone"])

# ── 2. each cap, each window: reached → next run blocked by that cap ──────
CASES = [
    ("tokens", "token_limit", "LLM token quota exceeded"),
    ("spend", "spend_limit_usd", "LLM spend quota exceeded"),
    ("queries", "query_limit", "data queries"),
    ("data_bytes", "data_bytes_limit", "data scanned"),
]
for window in ("daily", "monthly", "both"):
    for metric, field, phrase in CASES:
        now_day, now_month = used_today(), used_month()
        cap_day = now_day[metric]
        cap_month = now_month[metric]
        if metric in ("tokens", "queries", "data_bytes"):
            cap_day, cap_month = int(cap_day), int(cap_month)
        if window == "daily":
            limits = {f"daily_{field}": cap_day}
            expect = "Daily"
        elif window == "monthly":
            limits = {f"monthly_{field}": cap_month}
            expect = "Monthly"
        else:  # both set; daily is the one reached
            limits = {f"daily_{field}": cap_day, f"monthly_{field}": cap_month * 100 + 10**6}
            expect = "Daily"
        set_policy(**limits)
        run = run_prompt(f"{window} {metric}")
        if metric in ("queries", "data_bytes") and window == "monthly":
            # Monthly data caps keep their pre-existing wording.
            hit = "Monthly usage quota exceeded" in run["_blob"]
        else:
            hit = f"{expect} " in run["_blob"] and (phrase in run["_blob"])
        after = used_today()
        check(f"{window} {metric} cap reached → run blocked by the {expect.lower()} cap", hit,
              limits=limits, run_status=run["status"], message=run["message"][:240],
              tools=run["tools"][-3:], grew_by=round(after[metric] - now_day[metric], 6))

set_policy()  # leave the org uncapped
report = {"timezone": a.timezone, "passed": sum(r["pass"] for r in results), "total": len(results),
          "results": results}
print(json.dumps(report, indent=2, default=str))
sys.exit(0 if all(r["pass"] for r in results) else 1)
