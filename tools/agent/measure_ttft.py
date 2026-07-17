#!/usr/bin/env python3
"""Measure submit -> first-token latency growth across a long report.

Feedback loop: docs/feedback-loops/report-context-ttft-growth.md

Drives POST /api/reports/{id}/completions (stream=true) for N turns on one
report and records, per turn:

  t_headers      HTTP response headers received
  t_started      completion.started SSE event
  t_first_block  first block.upsert  (planner skeleton -> context build done)
  t_first_delta  first block.delta.* (first LLM token surfaced to the client)
  t_finished     completion.finished

Backend log offsets are stored per turn so the [stream:] / [agent:] /
[context_hub:] phase lines can be attributed to each turn afterwards. Combine
with plan_decisions.metrics_json (prompt/cache tokens, first_token_ms) for the
provider-side view.

Usage (backend running, org seeded, an LLM configured):

    cd backend && uv run python ../tools/agent/measure_ttft.py \
        --data-source-id <ds_id> [--report-id <existing>] [--turns 16]

Auth defaults match tools/agent/seed_org.py (admin@example.com).
"""
import argparse
import json
import os
import time

import httpx

PROMPTS = [
    "Create a data table of total sales by billing country, sorted descending.",
    "Create a dashboard artifact that shows total sales by billing country as a bar chart.",
    "Edit the artifact: add a headline title 'Global Sales' and a short intro paragraph.",
    "Create a data table of the top 10 artists by total revenue.",
    "Edit the artifact: add a second section with the top 10 artists by revenue as a table.",
    "Create a data table of monthly revenue over time.",
    "Edit the artifact: add a line chart of monthly revenue over time.",
    "Create a data table of revenue by media type.",
    "Edit the artifact: add a pie or bar breakdown of revenue by media type.",
    "Create a data table of the top 10 customers by lifetime spend.",
    "Edit the artifact: add a section listing the top 10 customers by lifetime spend.",
    "Create a data table of average invoice total by country.",
    "Edit the artifact: add a chart of average invoice total by country.",
    "Summarize everything in the report so far in a short narrative paragraph.",
    "Create a data table of track count by genre.",
    "Edit the artifact: add a genre distribution chart.",
]


def log_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def run_turn(client: httpx.Client, base: str, headers: dict, log_path: str,
             report_id: str, idx: int, prompt: str) -> dict:
    rec = {"turn": idx, "prompt": prompt, "log_offset_start": log_size(log_path), "events": []}
    body = {"prompt": {"content": prompt, "mode": "chat"}, "stream": True}
    t0 = time.monotonic()
    rec["t_submit_epoch"] = time.time()
    milestones = {"completion.started": None, "block.upsert": None,
                  "block.delta": None, "completion.finished": None}
    ev_count = 0
    with client.stream(
        "POST", f"{base}/api/reports/{report_id}/completions", json=body,
        headers={**headers, "Accept": "text/event-stream"},
        timeout=httpx.Timeout(1200, connect=30),
    ) as r:
        rec["status"] = r.status_code
        rec["t_headers_ms"] = (time.monotonic() - t0) * 1000
        for line in r.iter_lines():
            now = (time.monotonic() - t0) * 1000
            if line.startswith("event:"):
                ev = line.split(":", 1)[1].strip()
                ev_count += 1
                if ev_count <= 400:
                    rec["events"].append([round(now), ev])
                key = "block.delta" if ev.startswith("block.delta") else ev
                if key in milestones and milestones[key] is None:
                    milestones[key] = now
            elif line.startswith("data: [DONE]"):
                break
    rec["t_started_ms"] = milestones["completion.started"]
    rec["t_first_block_ms"] = milestones["block.upsert"]
    rec["t_first_delta_ms"] = milestones["block.delta"]
    rec["t_finished_ms"] = milestones["completion.finished"]
    rec["n_events"] = ev_count
    rec["log_offset_end"] = log_size(log_path)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--email", default="admin@example.com")
    ap.add_argument("--password", default="Password123!")
    ap.add_argument("--data-source-id", help="attach this data source to a new report")
    ap.add_argument("--report-id", help="continue an existing report instead of creating one")
    ap.add_argument("--turns", type=int, default=16)
    ap.add_argument("--out", default="/tmp/bow-agent/ttft_results.jsonl")
    ap.add_argument("--backend-log", default="/tmp/bow-agent/backend.log")
    args = ap.parse_args()

    client = httpx.Client(timeout=60)
    tok = client.post(f"{args.base_url}/api/auth/jwt/login",
                      data={"username": args.email, "password": args.password}).json()["access_token"]
    orgs = client.get(f"{args.base_url}/api/organizations",
                      headers={"Authorization": f"Bearer {tok}"}).json()
    headers = {"Authorization": f"Bearer {tok}", "X-Organization-Id": orgs[0]["id"]}

    report_id = args.report_id
    if not report_id:
        payload = {"title": "TTFT probe report", "files": [],
                   "data_sources": [args.data_source_id] if args.data_source_id else []}
        r = client.post(f"{args.base_url}/api/reports", json=payload, headers=headers)
        r.raise_for_status()
        report_id = r.json()["id"]
        print(f"report_id={report_id}", flush=True)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "a") as f:
        for i in range(args.turns):
            prompt = PROMPTS[i % len(PROMPTS)]
            print(f"--- turn {i}: {prompt[:60]}", flush=True)
            try:
                rec = run_turn(client, args.base_url, headers, args.backend_log,
                               report_id, i, prompt)
            except Exception as e:
                rec = {"turn": i, "error": repr(e)}
            rec["report_id"] = report_id
            f.write(json.dumps(rec) + "\n")
            f.flush()
            print("    " + str({k: rec.get(k) for k in (
                "t_headers_ms", "t_started_ms", "t_first_block_ms",
                "t_first_delta_ms", "t_finished_ms", "n_events", "error")}), flush=True)
            time.sleep(1.0)


if __name__ == "__main__":
    main()
