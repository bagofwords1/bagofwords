#!/usr/bin/env python3
"""Live probe: create_data's visualization inference runs off the agent loop.

Sends one chat turn like the browser (POST /api/reports/{id}/completions,
SSE), timestamps the events that matter, then checks what was persisted.

What it asserts (exit 1 on any violation):

* every create_data result that ended with ``viz_pending: true`` gets exactly
  one ``visualization.refined`` event, before ``completion.finished``;
* no other tool starts between a pending create_data and its refinement
  (the agent settles before a tool batch that may read visualizations);
* after the run the database holds the refined spec: the tool execution's
  ``result_json`` has no ``viz_pending`` and the same data_model/view as the
  event, the step's data_model and the visualization's view match it.

What it measures (printed as one JSON line):

* ``create_data_ms`` — each create_data tool's duration (tool.finished);
* ``refine_after_tool_ms`` — how long after tool.finished the refinement
  landed (time the planner no longer waits for);
* ``planner_resumed_ms`` — tool.finished -> the next planner event;
* ``total_ms`` — the whole turn.

    cd backend && uv run python ../tools/agent/viz_refine_probe.py \
        --prompt "Chart revenue by billing country" --db db/agent.db

Run it once with the backend started normally and once with
BOW_VIZ_INFERENCE_INLINE=1 to compare against the inline pass (the probe then
sees no pending results and only reports timings).
"""
import argparse
import json
import os
import sqlite3
import sys
import time

import httpx


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default=os.environ.get("BOW_BASE_URL", "http://localhost:8000"))
    p.add_argument("--email", default=os.environ.get("BOW_EMAIL", "admin@example.com"))
    p.add_argument("--password", default=os.environ.get("BOW_PASSWORD", "Password123!"))
    p.add_argument("--prompt", required=True)
    p.add_argument("--label", default="")
    p.add_argument("--db", default="", help="SQLite app database, to check persisted state")
    p.add_argument("--stop-after-create-data", action="store_true",
                   help="POST a stop right after the first create_data finishes")
    a = p.parse_args()

    c = httpx.Client(base_url=a.base.rstrip("/"), timeout=httpx.Timeout(30, read=600), trust_env=False)
    tok = c.post("/api/auth/jwt/login", data={"username": a.email, "password": a.password}).json()["access_token"]
    org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
    H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}
    sources = c.get("/api/data_sources", headers=H).json()
    ds_ids = [s["id"] for s in sources if isinstance(s, dict) and s.get("id")]
    report = c.post("/api/reports", json={"title": f"viz probe {a.label}".strip(), "data_sources": ds_ids}, headers=H).json()

    events: list[dict] = []
    t0 = time.monotonic()
    stop_sent = False
    with c.stream(
        "POST", f"/api/reports/{report['id']}/completions",
        json={"prompt": {"content": a.prompt, "mentions": [], "mode": "chat"}, "stream": True},
        headers={**H, "Accept": "text/event-stream", "Cache-Control": "no-cache"},
    ) as r:
        buf, name = "", None
        for chunk in r.iter_text():
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.rstrip("\r")
                if line.startswith("event:"):
                    name = line[6:].strip()
                elif line.startswith("data:"):
                    raw = line[5:].strip()
                    now = (time.monotonic() - t0) * 1000.0
                    if raw == "[DONE]":
                        events.append({"t": now, "event": "[DONE]", "data": {}})
                        continue
                    try:
                        parsed = json.loads(raw)
                    except Exception:
                        continue
                    data = parsed.get("data", parsed) if isinstance(parsed, dict) else {}
                    events.append({"t": now, "event": name, "data": data,
                                   "completion_id": parsed.get("completion_id") if isinstance(parsed, dict) else None})
                    if (a.stop_after_create_data and not stop_sent and name == "tool.finished"
                            and data.get("tool_name") == "create_data"):
                        cid = parsed.get("completion_id")
                        if cid:
                            c.post(f"/api/completions/{cid}/sigkill", headers=H)
                            stop_sent = True
    total = (time.monotonic() - t0) * 1000.0

    errors: list[str] = []
    finished = [e for e in events if e["event"] == "tool.finished"]
    refined = [e for e in events if e["event"] == "visualization.refined"]
    completion_finished = next((e for e in events if e["event"] == "completion.finished"), None)
    planner_events = {"decision.partial", "decision.final", "block.delta.token", "block.delta.text"}

    cd_rows = []
    for f in finished:
        d = f["data"]
        if d.get("tool_name") != "create_data":
            continue
        rj = d.get("result_json") or {}
        te_id = d.get("tool_execution_id")
        pending = rj.get("viz_pending") is True
        refs = [e for e in refined if e["data"].get("tool_execution_id") == te_id]
        nxt = next((e for e in events if e["t"] > f["t"] and e["event"] in planner_events), None)
        row = {
            "tool_execution_id": te_id,
            "type": (rj.get("data_model") or {}).get("type"),
            "pending": pending,
            "create_data_ms": d.get("duration_ms"),
            "planner_resumed_ms": round(nxt["t"] - f["t"]) if nxt else None,
        }
        if pending:
            if len(refs) != 1:
                errors.append(f"{te_id}: {len(refs)} visualization.refined events (want 1)")
            else:
                ref = refs[0]
                row["refine_after_tool_ms"] = round(ref["t"] - f["t"])
                row["refined"] = ref["data"].get("refined")
                if completion_finished and ref["t"] > completion_finished["t"]:
                    errors.append(f"{te_id}: refined after completion.finished")
                # No other tool may start between this result and its refinement.
                for e in events:
                    if (e["event"] == "tool.started" and f["t"] < e["t"] < ref["t"]
                            and e["data"].get("tool_name") != "create_data"
                            and e["data"].get("tool_name") != "inspect_data"):
                        errors.append(f"{te_id}: {e['data'].get('tool_name')} started before the refinement landed")
                row["_ref"] = ref["data"]
        elif refs:
            errors.append(f"{te_id}: refined event for a result that was not pending")
        row["step_id"] = d.get("created_step_id")
        row["visualization_id"] = (d.get("created_visualization_ids") or [None])[0]
        cd_rows.append(row)

    # Persisted state.
    if a.db and os.path.exists(a.db):
        con = sqlite3.connect(a.db)
        for row in cd_rows:
            ref = row.pop("_ref", None)
            te = con.execute("select result_json, context_summary_json from tool_executions where id=?",
                             (row["tool_execution_id"],)).fetchone()
            rj = json.loads(te[0]) if te and te[0] else None
            summary = json.loads(te[1]) if te and te[1] else None
            if rj is None:
                errors.append(f"{row['tool_execution_id']}: tool execution not persisted")
                continue
            if "viz_pending" in rj:
                errors.append(f"{row['tool_execution_id']}: result_json still has viz_pending")
            if ref and ref.get("refined"):
                if rj.get("data_model") != ref.get("data_model"):
                    errors.append(f"{row['tool_execution_id']}: result_json.data_model != refined")
                if rj.get("view") != ref.get("view"):
                    errors.append(f"{row['tool_execution_id']}: result_json.view != refined")
                # What a report load serves (context_summary_json projection).
                if isinstance(summary, dict) and (summary.get("data_model") != ref.get("data_model")
                                                  or summary.get("view") != ref.get("view")):
                    errors.append(f"{row['tool_execution_id']}: context_summary_json not refined (reload would show the deterministic chart)")
                st = con.execute("select data_model from steps where id=?", (row["step_id"],)).fetchone()
                sdm = json.loads(st[0]) if st and st[0] else {}
                for k in ("type", "series", "group_by", "filters"):
                    if sdm.get(k) != ref["data_model"].get(k):
                        errors.append(f"{row['step_id']}: step.data_model.{k} != refined")
                vz = con.execute("select view from visualizations where id=?", (row["visualization_id"],)).fetchone()
                vview = json.loads(vz[0]) if vz and vz[0] else {}
                if (vview.get("view") or {}).get("type") != (ref["view"].get("view") or {}).get("type"):
                    errors.append(f"{row['visualization_id']}: visualization.view type != refined")
                if (vview.get("view") or {}).get("y") != (ref["view"].get("view") or {}).get("y"):
                    errors.append(f"{row['visualization_id']}: visualization.view.y != refined")
                row["persisted"] = "refined"
            else:
                row["persisted"] = "deterministic" if row["pending"] else "inline"
    for row in cd_rows:
        row.pop("_ref", None)

    tools = [e["data"].get("tool_name") for e in events if e["event"] == "tool.started"]
    out = {
        "label": a.label,
        "report_id": report["id"],
        "total_ms": round(total),
        "status": (completion_finished or {}).get("data", {}).get("status"),
        "tools": tools,
        "create_data": cd_rows,
        "stop_sent": stop_sent,
        "errors": errors,
    }
    print(json.dumps(out))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
