#!/usr/bin/env python3
"""Drive real chat turns against a large, slow mock Priority tenant.

Sandbox tool (not a test): proves the Priority connector answers line-level
questions without hanging. Needs a running backend with an LLM configured
(seed_org.py + setup_haiku_llm.py) and the mock in large-tenant mode:

    uv run python ../tools/priority/mock_server.py --orders 6000 --log /tmp/prio.jsonl
    uv run python ../tools/agent/priority_large_tenant_e2e.py --db-path db/prio.db

Each prompt runs in its own report. For every turn it prints the status, wall
time, each data tool's queries/errors, and the final answer, read back from
the database. Prompts are ordinary user questions; none mentions how the
connector works.
"""
import argparse
import json
import sqlite3
import sys
import time

import httpx

PROMPTS = [
    "show me 5 rows from purchase order items",
    "what are the 10 most recent purchase order lines?",
    "Which 5 parts did we spend the most on in purchase orders over the last 3 months?",
    "top 5 most expensive purchase order lines",
    "תראה לי 5 שורות מפריטי הזמנות רכש",
]


def auth(token, org):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": org}


def turn_report(db_path, report_id):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    ae = c.execute("select * from agent_executions where report_id=? order by created_at desc limit 1",
                   (report_id,)).fetchone()
    comp = c.execute("select * from completions where report_id=? and role='system' order by created_at desc limit 1",
                     (report_id,)).fetchone()
    tools = c.execute("select * from tool_executions where agent_execution_id=? order by created_at",
                      (ae["id"],)).fetchall() if ae else []
    return ae, comp, tools


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--email", default="admin@example.com")
    ap.add_argument("--password", default="Password123!")
    ap.add_argument("--db-path", default="db/app.db")
    ap.add_argument("--service-root", default="http://127.0.0.1:8901/odata/Priority/tabmock.ini/demo")
    ap.add_argument("--data-source-id", help="reuse an existing Priority data source")
    ap.add_argument("--prompt", action="append", help="override the default prompts (repeatable)")
    ap.add_argument("--out", help="write the per-turn results as JSON here")
    args = ap.parse_args()

    client = httpx.Client(base_url=args.base_url, timeout=900)
    token = client.post("/api/auth/jwt/login",
                        data={"username": args.email, "password": args.password}).json()["access_token"]
    org = client.get("/api/organizations", headers={"Authorization": f"Bearer {token}"}).json()[0]["id"]
    H = auth(token, org)

    ds_id = args.data_source_id
    if not ds_id:
        r = client.post("/api/data_sources", headers=H, json={
            "name": "Priority ERP", "type": "priority_erp",
            "config": {"service_root": args.service_root},
            "credentials": {"pat": "DEMOTOKEN"}, "auth_policy": "system_only",
            "generate_summary": False, "generate_conversation_starters": False, "generate_ai_rules": False,
        })
        if r.status_code not in (200, 201):
            sys.exit(f"data source create failed: {r.status_code} {r.text[:400]}")
        ds_id = r.json()["id"]
        print("data source", ds_id)

    # A new connection indexes its forms with every table off; switch them on
    # as an admin would in the data source's table picker.
    for _ in range(60):
        tables = client.get(f"/api/data_sources/{ds_id}/full_schema", headers=H).json()
        tables = tables.get("tables", tables) if isinstance(tables, dict) else tables
        if tables:
            break
        time.sleep(2)
    r = client.post(f"/api/data_sources/{ds_id}/bulk_update_tables", headers=H, json={"action": "activate"})
    print("activate tables:", r.status_code, len(tables or []), "tables")

    results = []
    for prompt in args.prompt or PROMPTS:
        report = client.post("/api/reports", headers=H, json={
            "title": prompt[:60], "widget": None, "files": [], "data_sources": [ds_id]}).json()
        started = time.monotonic()
        r = client.post(f"/api/reports/{report['id']}/completions", headers=H, params={"background": False},
                        json={"prompt": {"content": prompt, "widget_id": None, "step_id": None, "mentions": [{}]}})
        wall = time.monotonic() - started
        ae, comp, tools = turn_report(args.db_path, report["id"])
        answer = (json.loads(comp["completion"] or "{}").get("content") or "") if comp else ""
        turn = {"prompt": prompt, "report_id": report["id"], "http": r.status_code,
                "status": ae["status"] if ae else None, "seconds": round(wall, 1), "tools": [],
                "answer": answer}
        for t in tools:
            res = json.loads(t["result_json"] or "{}") if t["result_json"] else {}
            sub = json.loads(t["sub_timings_json"] or "{}") if t["sub_timings_json"] else {}
            errors = [e[1] if isinstance(e, list) and len(e) > 1 else e for e in (res.get("errors") or [])]
            turn["tools"].append({
                "tool": t["tool_name"], "success": bool(t["success"]),
                "seconds": round((t["duration_ms"] or 0) / 1000, 1),
                "queries": [{"q": q.get("sql"), "ms": q.get("query_ms"), "rows": q.get("rows"),
                             "error": q.get("error")} for q in (sub.get("queries") or [])],
                "errors": [str(e)[:300] for e in errors] + ([t["error_message"][:300]] if t["error_message"] else []),
                "rows": (res.get("stats") or {}).get("total_rows"),
            })
        results.append(turn)
        print(f"\n=== {prompt}\n{turn['status']} in {turn['seconds']}s")
        for t in turn["tools"]:
            print(f"  {t['tool']}: success={t['success']} {t['seconds']}s rows={t['rows']}")
            for q in t["queries"]:
                print(f"    query {q['ms']}ms rows={q['rows']} {q['q']}" + (f"  ERROR {q['error']}" if q["error"] else ""))
            for e in t["errors"]:
                print(f"    error: {e}")
        print("  answer:", answer[:700].replace("\n", "\n          "))
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
