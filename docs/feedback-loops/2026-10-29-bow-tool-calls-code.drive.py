#!/usr/bin/env python3
"""Drive the bow.tool_calls `code` loop through the real API.

1. Chat report on the Music Store agent: the scripted model calls create_data,
   which runs SQL and saves a step with that code.
2. Training report: the model calls create_data on builtin:bow / bow.tool_calls
   and its code asks for the `code` column.
3. Compare: the training table's `code` for the chat call equals the chat step's code.

    cd backend && BOW_DATABASE_URL=sqlite:///db/<db> uv run python ../docs/feedback-loops/2026-10-29-bow-tool-calls-code.drive.py
"""
import json
import os
import sqlite3

import httpx

BASE = os.environ.get("BOW_BASE_URL", "http://localhost:8000")
DB = os.environ["BOW_DATABASE_URL"].replace("sqlite:///", "")
c = httpx.Client(base_url=BASE, timeout=None, trust_env=False)
tok = c.post("/api/auth/jwt/login", data={"username": "admin@example.com", "password": "Password123!"}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
c.headers.update({"Authorization": f"Bearer {tok}", "X-Organization-Id": org})
ds_id = c.get("/api/data_sources").json()[0]["id"]


def ask(title, mode, prompt, data_sources):
    report = c.post("/api/reports", json={"title": title, "mode": mode, "data_sources": data_sources}).json()
    body = {"prompt": {"content": prompt, "mode": mode}, "stream": True}
    with c.stream("POST", f"/api/reports/{report['id']}/completions", json=body,
                  headers={"Accept": "text/event-stream"}) as resp:
        for _ in resp.iter_lines():
            pass
    return report["id"]


def created_steps(report_id):
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT te.id, te.status, te.created_step_id, s.code FROM tool_executions te "
        "JOIN agent_executions ae ON ae.id = te.agent_execution_id "
        "LEFT JOIN steps s ON s.id = te.created_step_id "
        "WHERE ae.report_id = ? AND te.tool_name = 'create_data' ORDER BY te.created_at", (report_id,)).fetchall()
    con.close()
    return rows


chat_report = ask("Tracks by genre", "chat", "How many tracks per genre?", [ds_id])
chat_calls = created_steps(chat_report)
print("chat create_data calls:", [(r[0][:8], r[1], (r[2] or "")[:8]) for r in chat_calls])
call_id, _, step_id, step_code = chat_calls[-1]
assert step_id and step_code, "chat call created no step"

training_report = ask("Code check", "training", "CODECHECK: show recent create_data calls with the code they used", [])
t_call, t_status, t_step, t_code = created_steps(training_report)[-1]
print("training create_data:", t_status, "step", (t_step or "")[:8])
print("training step code:\n" + t_code)
step = c.get(f"/api/steps/{t_step}").json()
rows = (step.get("data") or {}).get("rows") or []
print("training table columns:", [col.get("field") for col in (step.get("data") or {}).get("columns", [])])
match = [r for r in rows if r.get("call_id") == call_id]
print("rows:", len(rows), "row for the chat call:", bool(match))
assert match, "chat call missing from bow.tool_calls"
print("code returned == chat step code:", match[0]["code"] == step_code)
print("--- code returned for the chat call ---\n" + match[0]["code"])
assert match[0]["code"] == step_code
print(json.dumps({"chat_report": chat_report, "training_report": training_report}))
