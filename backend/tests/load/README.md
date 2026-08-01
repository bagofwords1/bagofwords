# Report-activity load harness (manual)

Measures the live report-status pipeline (per-worker DB watcher → SSE fan-out
→ snapshot endpoint) at scale, with the LLM fully mocked: "runs" are direct
DB state transitions, which is exactly what the watcher/stream/snapshot read.

## Setup

1. Postgres (the scale configuration; sqlite serializes concurrent reads):
   `initdb + pg_ctl start`, create a DB, `alembic upgrade head` against it.
2. Backend: `BOW_DATABASE_URL=postgresql://... python -m uvicorn main:app`.
3. Register one admin via `POST /api/auth/register` (creates the org), then
   `python seed_scale.py` — 100 users, 500 reports (writes `scale_ids.txt`).

## Run

- `python activity_load.py` — 100 concurrent SSE streams; 500 reports
  flip to running in one transaction (mass-burst worst case), then all
  finish; snapshot endpoint hammered by 100 users concurrently.
- `python single_event_latency.py` — the steady-state number: one report
  changes, measure delivery to all 100 connected clients.

## Results (2026-08-01, single uvicorn worker, local Postgres 16)

| Metric | Result |
|---|---|
| Fan-out completeness | 100 clients × 1000 events each — 0 drops, 0 errors |
| Single-change delivery (steady state) | p50 = p95 ≈ 1.1s to all 100 clients |
| Mass burst (500 reports at once) | p50 2.9s, p95 3.7s, max 3.9s |
| Snapshot GET /reports/activity, 100 ids, uncontended | ~25 ms |
| Snapshot storm (100 concurrent × 100 ids) | wall 2.2s (~50 req/s/worker, Python serialization-bound) |

Notes: delivery latency floor is the watcher tick (2s); burst numbers include
processing 500 candidates/tick. The storm case only occurs on mass reconnect
(e.g. after a deploy) and scales with worker count. QUEUE_MAXSIZE (2048) must
stay ≥ MAX_CANDIDATES (1000) so a full burst never drops events.
