#!/usr/bin/env python3
"""Demo-data eval: drive real training-mode completions per use case, approve
the create_demo_dataset review card through the real endpoint, then score the
generated SQLite file.

Use cases: hr, finance, sales, monitoring (add more in CASES).

    cd backend && uv run python ../tools/agent/demo_data_eval.py \
        --token <admin token> --org <org id> --case hr --case finance --case sales --case monitoring

The org's default + small default model decide the result (configure e.g.
Claude Haiku 5.5 for both). Writes <out>/<case>.json and prints a markdown
scorecard. Exit code 1 if any case fails a hard check.

Hard checks (must pass): dataset created; >= 3 tables; every table non-empty;
PRAGMA foreign_key_check clean; no dates after today (unless the column is a
due/expected/scheduled one); at least one agent created with an emoji icon.
Soft signals (reported): numeric skew (not uniform), category mix (not
uniform), monthly volume variation (seasonality/trend), null usage, retries.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
from datetime import date

import httpx

CASES = {
    "hr": "Create a demo dataset for HR analytics at a 400-person software company (headcount, hiring, attrition, compensation). Suggest agents on top.",
    "finance": "I want a demo for finance in the e-commerce sector: orders, payments, refunds, chargebacks and payouts over the last 2 years. Build it and suggest agents.",
    "sales": "Build a demo dataset for a B2B SaaS sales team: accounts, contacts, opportunities through pipeline stages, activities and closed deals. Add suggested agents.",
    "monitoring": "Create demo data for monitoring a microservices platform: services, hosts, request logs with severities, and per-minute latency/error metrics with a couple of incidents. Suggest agents.",
}

FUTURE_OK = re.compile(r"(due|expir|expected|scheduled|planned|forecast|renewal|next|valid_to|until)", re.I)


def drive(base: str, headers: dict, name: str, prompt: str, timeout_s: int) -> dict:
    c = httpx.Client(base_url=base, headers=headers, timeout=None)
    rep = c.post("/api/reports", json={"title": f"Demo data eval – {name}", "mode": "training", "data_sources": []})
    rep.raise_for_status()
    report_id = rep.json()["id"]
    t0 = time.time()
    log = {"case": name, "report_id": report_id, "decisions": []}
    completion_ids: set = set()
    body = {"prompt": {"content": prompt, "mode": "training"}, "stream": True}
    with c.stream("POST", f"/api/reports/{report_id}/completions", json=body,
                  headers={"Accept": "text/event-stream"}) as resp:
        event = None
        for line in resp.iter_lines():
            if time.time() - t0 > timeout_s:
                log["timeout"] = True
                break
            if line.startswith("event:"):
                event = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            try:
                data = json.loads(line[5:].strip())
            except Exception:
                continue
            if isinstance(data, dict) and "event" in data and isinstance(data.get("data"), dict):
                event = data["event"]
                if data.get("completion_id"):
                    completion_ids.add(data["completion_id"])
                data = data["data"]
            if event == "tool.confirmation" and data.get("tool_name") == "create_demo_dataset":
                payload = data.get("payload") or {}
                cid = payload.get("confirmation_id")
                try:
                    cl = c.get(f"/api/reports/{report_id}/completions").json()
                    cl = cl if isinstance(cl, list) else (cl.get("completions") or cl.get("items") or [])
                    completion_ids.update(x["id"] for x in cl if x.get("id"))
                except Exception:
                    pass
                decision = {"approved": True, "remember": False, "response": {"agents": payload.get("agents") or []}}
                ok = any(
                    c.post(f"/api/completions/{comp}/mcp_tool_confirmations/{cid}", json=decision).status_code == 200
                    for comp in list(completion_ids)
                )
                log["decisions"].append({"at_s": round(time.time() - t0, 1), "sent": ok})
    log["seconds"] = round(time.time() - t0, 1)
    comps = c.get(f"/api/reports/{report_id}/completions").json()
    items = comps if isinstance(comps, list) else comps.get("completions") or comps.get("items") or []
    calls, tools = [], []
    for comp in items:
        for b in (comp.get("completion_blocks") or []):
            te = b.get("tool_execution") or {}
            if te.get("tool_name"):
                tools.append(te["tool_name"])
            if te.get("tool_name") == "create_demo_dataset":
                rj = te.get("result_json") or {}
                calls.append({"args": te.get("arguments_json") or {}, "result": rj.get("output", rj)})
    log["tools"] = tools
    log["demo_calls"] = calls
    return log


def score_sqlite(path: str, spec: dict) -> dict:
    out = {"tables": {}, "hard": [], "soft": {}}
    if not path or not os.path.exists(path):
        out["hard"].append("sqlite file missing")
        return out
    conn = sqlite3.connect(path)
    try:
        fk_bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_bad:
            out["hard"].append(f"{len(fk_bad)} foreign key violations")
        today = date.today().isoformat()
        skewed = numeric = cats = mixed = seasonal = timed = nulls_used = 0
        for t in spec.get("tables", []):
            name = t["name"]
            n = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            out["tables"][name] = n
            if n == 0:
                out["hard"].append(f"{name}: empty")
                continue
            for col in t.get("columns", []):
                cn, ty = col["name"], col["type"]
                if col.get("nullable"):
                    if conn.execute(f'SELECT COUNT(*) FROM "{name}" WHERE "{cn}" IS NULL').fetchone()[0]:
                        nulls_used += 1
                if ty in ("integer", "real") and not col.get("primary_key") and not col.get("references"):
                    vals = [r[0] for r in conn.execute(f'SELECT "{cn}" FROM "{name}" WHERE "{cn}" IS NOT NULL LIMIT 20000')]
                    if len(vals) > 20 and len(set(vals)) > 5:
                        numeric += 1
                        m = sum(vals) / len(vals)
                        sd = math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals)) or 1e-9
                        skew = sum(((v - m) / sd) ** 3 for v in vals) / len(vals)
                        if abs(skew) > 0.3:
                            skewed += 1
                if ty == "text" and not col.get("primary_key") and not col.get("references"):
                    rows = conn.execute(f'SELECT "{cn}", COUNT(*) FROM "{name}" GROUP BY 1').fetchall()
                    if 2 <= len(rows) <= 30 and n >= 20:
                        cats += 1
                        shares = sorted((c / n for _, c in rows), reverse=True)
                        if shares[0] > 1.5 / len(rows):
                            mixed += 1
                if ty in ("date", "datetime"):
                    mx = conn.execute(f'SELECT MAX("{cn}") FROM "{name}"').fetchone()[0]
                    if mx and str(mx)[:10] > today and not FUTURE_OK.search(f"{cn} {col.get('description', '')}"):
                        out["hard"].append(f"{name}.{cn}: future dates (max {mx})")
                    if n >= 200:
                        months = conn.execute(f'SELECT substr("{cn}",1,7), COUNT(*) FROM "{name}" GROUP BY 1').fetchall()
                        if len(months) >= 6:
                            timed += 1
                            vals = [c for _, c in months[1:-1]] or [c for _, c in months]
                            m = sum(vals) / len(vals)
                            cv = math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals)) / (m or 1)
                            if cv > 0.08:
                                seasonal += 1
        out["soft"] = {
            "numeric_skewed": f"{skewed}/{numeric}",
            "categories_weighted": f"{mixed}/{cats}",
            "time_cols_with_variation": f"{seasonal}/{timed}",
            "nullable_cols_with_nulls": nulls_used,
        }
    finally:
        conn.close()
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://localhost:8000")
    p.add_argument("--token", required=True)
    p.add_argument("--org", required=True)
    p.add_argument("--case", action="append", required=True, choices=sorted(CASES))
    p.add_argument("--out", default="demo_data_eval_out")
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--backend-dir", default=os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
    a = p.parse_args()
    os.makedirs(a.out, exist_ok=True)
    headers = {"Authorization": f"Bearer {a.token}", "X-Organization-Id": a.org}
    api = httpx.Client(base_url=a.base, headers=headers, timeout=60)

    rows, failed = [], False
    for case in a.case:
        log = drive(a.base, headers, case, CASES[case], a.timeout)
        created = [x for x in log["demo_calls"] if (x["result"] or {}).get("status") == "created"]
        res = created[-1]["result"] if created else ((log["demo_calls"][-1]["result"] if log["demo_calls"] else {}) or {})
        spec = created[-1]["args"] if created else {}
        hard, soft, path = [], {}, None
        if not created:
            hard.append(f"not created (status={res.get('status')}, tools={log['tools']})")
        else:
            conn = api.get(f"/api/connections/{res['connection_id']}").json()
            cfg = conn.get("config") or {}
            cfg = json.loads(cfg) if isinstance(cfg, str) else cfg
            path = cfg.get("database")
            if path and not os.path.isabs(path):
                path = os.path.normpath(os.path.join(a.backend_dir, path))
            sc = score_sqlite(path, spec)
            hard += sc["hard"]
            soft = sc["soft"]
            if len(spec.get("tables", [])) < 3:
                hard.append("fewer than 3 tables")
            agents = res.get("agents") or []
            if not agents:
                hard.append("no agents created")
            elif not all((ag.get("icon") or "").startswith("emoji:") for ag in agents):
                hard.append("agent without emoji icon")
        log["score"] = {"hard_failures": hard, "soft": soft}
        json.dump(log, open(os.path.join(a.out, f"{case}.json"), "w"), indent=2, default=str)
        failed |= bool(hard)
        rows.append({
            "case": case, "ok": not hard, "seconds": log["seconds"],
            "tables": ", ".join(f"{t['name']} {t['rows']:,}" for t in (res.get("tables") or [])),
            "retries": sum(max(0, t.get("attempts", 1) - 1) for t in (res.get("tables") or [])),
            "agents": ", ".join(f"{(ag.get('icon') or '').replace('emoji:', '')} {ag['name']} ({len(ag.get('active_tables') or [])})" for ag in (res.get("agents") or [])),
            "soft": soft, "hard": hard,
        })

    print("| case | result | time | tables (rows) | table retries | agents (active tables) | skewed numerics | weighted categories | time variation |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        s = r["soft"] or {}
        print(f"| {r['case']} | {'PASS' if r['ok'] else 'FAIL: ' + '; '.join(r['hard'])} | {r['seconds']:.0f}s | {r['tables']} | {r['retries']} | "
              f"{r['agents']} | {s.get('numeric_skewed', '-')} | {s.get('categories_weighted', '-')} | {s.get('time_cols_with_variation', '-')} |")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
