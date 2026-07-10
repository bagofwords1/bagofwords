# Agent latency deep-dive — where every millisecond of a completion goes

**Question investigated:** a simple conversation over the Prometheus connector
("which scrape targets are down?") showed `Created Data … 129.6s` in the UI.
Why? This report accounts for the full prompt→response path — planner, agent
loop, `create_data` (codegen / execution / persistence), title, follow-ups,
judge — measured on both **SQLite** and **Postgres** backends.

**No fixes are implemented here.** This is a diagnosis document; candidate
directions are listed at the end.

## TL;DR

The user-visible `Created Data 129.6s` (and 65s on every later run) is **not**
the LLM, **not** the Prometheus query, and **not** the connector:

| Component | Actual cost |
|---|---:|
| Prometheus query (`up`, 6 rows) | **67 ms** |
| Connector schema build (`create_data.schema_build`) | 26 ms |
| Coder LLM call (writes the pandas code) | 3.3–5.3 s |
| **Two usage-metering DB writes on SQLite** | **60.2 s (2 × 30 s)** |

Each sandboxed `execute_query()` is wrapped with usage-policy metering: one
bookkeeping write **before** the query (`_consume_query_quota`) and one
**after** (`_consume_data_bytes_quota`). On SQLite, both writes queue behind
the agent's writer lock, wait out **`PRAGMA busy_timeout = 30000`** (30s) each,
fail with `database is locked`, and are **skipped** — burning 60.2s per
execution to record nothing. On Postgres the same writes take single-digit
milliseconds and `executing_code` drops from **60,196 ms → 153 ms (~400×)**.

The original 129.6s run was this 65s pattern **twice**: the streaming executor
did an internal codegen retry (two full generate+execute cycles), not API
jitter as first assumed.

## Method

- **Persisted stage timings**: `tool_executions.sub_timings_json` records
  per-stage ms (`tool_runner.py:171-193`) including `codegen_ms`,
  `execution_ms`, and per-query `query_ms` — this data was already in the DB
  for every historical run.
- **py-spy thread dumps** every 4s across a live run (backend PID attached,
  no code changes), classifying which frame each stall was in.
- **LLM call ledger**: `llm_usage_records` timestamps + token counts per call.
- **Differential experiment**: identical prompt, identical Prometheus stack,
  identical model (claude-haiku-4-5), backend switched SQLite → Postgres 16.

## The full prompt→response timeline (SQLite, profiled run)

Report `5ce41ba5`, prompt: *"Which scrape targets are currently down?"*
`agent_execution.total_duration_ms = 76.7s`:

| t | Phase | Cost | Notes |
|---|---|---:|---|
| 0.0s | HTTP POST → completion created, SSE stream opens | ~0.3s | `completion_service` |
| 0.3s | Context hub warm-up (queries/mentions/entities/messages) | ~0.2s | logged `refresh_warm all_done +41ms` |
| 0.5s | `judge.instructions_context` LLM call | ~1.5s | in=405 out=149 |
| 2.0s | **Planner call #1** (decision: `create_data`) | ~2.5s | in=2,127 out=263 |
| 4.5s | `create_data` tool starts | | `tool_executions.started_at` |
| | ├ init / resolve tables / init code exec | 270 ms | |
| | ├ `create_data.schema_build` (connector) | **26 ms** | resolve_active 13ms + final_excerpt 13ms |
| | ├ `generating_code` — coder LLM writes pandas code | 3,305 ms | usage row **missing on SQLite** (see below) |
| | ├ `executing_code` | **60,196 ms** | ↓ breakdown |
| | │ ├ `_consume_query_quota` write → **blocked 30s** | ~30,000 ms | py-spy dumps 08:03:52→08:04:16 |
| | │ ├ **actual Prometheus query `up`** | **~70 ms** | 6 rows, 992 bytes |
| | │ └ `_consume_data_bytes_quota` write → **blocked 30s** | ~30,000 ms | py-spy dumps 08:04:20→08:04:48 |
| | └ post_execution + formatting_widget | 15 ms | |
| 69.4s | **Planner call #2** (observe result → final answer) | ~2.5s | in=4,229 out=230 |
| 72s | `report.title` | ~2.8s | in=631 out=10 |
| 75s | `report.follow_ups` | ~2.2s | in=2,416 out=94 |
| 78s | `judge.response_quality` (background) | ~3.6s | in=1,919 out=118 |

**~78% of the wall clock is the two 30s metering stalls.** Everything else —
five LLM calls plus all orchestration — fits in ~17s.

## The py-spy proof

The sandbox worker thread during the stall (dump `run_03_080352`):

```
Thread 19919 "bow_code_exec_0"
    wait (threading.py:355)
    result (concurrent/futures/_base.py:451)
    run_blocking (usage_policy_service.py:397)          ← blocks sandbox thread on the event loop
    _consume_query_quota (code_execution.py:492)        ← usage metering, BEFORE the query
    execute_query (code_execution.py:407)               ← the tracking wrapper
    generate_df (<string>:9)                            ← the generated code's execute_query('up')
    …
```

Dump classification across the run (one dump / 4s):

```
08:03:52 … 08:04:16   7 dumps: blocked in _consume_query_quota      (≈30s)
08:04:20 … 08:04:48   8 dumps: blocked in _consume_data_bytes_quota (≈30s)
```

The real Prometheus query executed in the <4s gap between the two windows
(persisted `query_ms` puts it at ~70ms; `query_ms` *includes* the first stall —
it is measured from before `_consume_query_quota`, `code_execution.py:402-409`,
which is why every run reports `query_ms ≈ 30,07x`).

## Root-cause chain

1. **Single-writer agent architecture** — always on for SQLite
   (`agent_v2.py:1685-1703`): all agent writes route through one session.
   `_release_db_between_steps()` (`agent_v2.py:1865`, called right before
   `tool_runner.run`, `agent_v2.py:3053`) commits the main session before the
   tool — but writes issued *during* the tool's own event stream
   (`_handle_streaming_event`, block upserts for streaming progress/code
   chunks) reopen a write transaction on the WAL while the tool is running.
2. **Metering writes from the sandbox thread** — the `execute_query` wrapper
   (`code_execution.py:397-449`) calls `_consume_query_quota` before and
   `_consume_data_bytes_quota` after every query. Each submits an async DB
   write to the event loop via `run_blocking` (`usage_policy_service.py:395`)
   and blocks the sandbox thread on `Future.result()`.
3. **`PRAGMA busy_timeout = 30000`** (`database.py:18`) — each metering write
   waits up to 30s for the WAL writer lock before erroring.
4. **Best-effort failure handling** (`code_execution.py:499-507`): the
   `OperationalError` is caught and the write skipped — *"Metering is
   best-effort there"* — so each stall burns 30s and then records **nothing**.
5. The pattern is per-execution-attempt. The 129.6s run had two internal
   codegen attempts (raw stage list shows `generating_code`/`executing_code`
   twice: 4.1s+60.3s then 3.8s+60.2s) — `retry_count=0` at the tool level, the
   retry is inside `generate_and_execute_stream_v2`.

### Collateral damage beyond latency (same lock contention)

- **Coder LLM usage rows are dropped on SQLite**: the PG run records
  `create_data.code_gen` usage (in=5,181/5,653 out=436/437); the SQLite runs
  have **no coder usage rows at all** — the usage recorder hits the same lock
  and skips (cost accounting undercounts codegen spend on SQLite).
- **Query metering is lost** (`usage_events`/`usage_counters` writes skipped),
  so data-query quota enforcement quietly doesn't meter on SQLite under load.

## SQLite vs Postgres — same prompt, same stack, same model

| Metric | SQLite | Postgres 16 | Δ |
|---|---:|---:|---|
| `executing_code` | 60,196 ms | **153 / 178 ms** | **~400× faster** |
| `query_ms` (`up`, 6 rows) | 30,078 ms* | **67 ms** | *includes 30s stall |
| `generating_code` (coder LLM) | 3,305 ms | 4,423 / 5,302 ms | LLM-bound, comparable |
| `create_data` total | 64.7 s | **13.1 s** | incl. 2 codegen cycles on PG |
| Whole agent run | 76.7 s | **41.7 s** | |
| Coder usage rows recorded | ❌ dropped | ✅ recorded | |

On Postgres the run is essentially **100% LLM-bound**: the 41.7s decomposes
into 11 LLM calls (judge 1.2s, 2× codegen ≈ 10s, 4 planner calls ≈ 22s — three
of them with ~29k-token prompts post-observation — title/follow-ups/judge ≈
6s). Inter-call orchestration and all DB work are milliseconds.

### Remaining (legitimate) latency profile on Postgres

The next lever after the SQLite pathology is prompt size: post-observation
planner calls carry **~29k input tokens** (in=29,173 / 29,283 / 29,332;
out≈200–500 each, ~4–7s per call). That is schema excerpt + observation +
history accumulation — a prompt-budget question, not a defect.

## Corrections to earlier in-flight conclusions

- ❌ *"~54s of run 1 was transient Anthropic API slowness"* → it was a second
  internal codegen+execute cycle (retry), each cycle carrying the fixed 60.2s
  stall.
- ❌ *"the ~65s is one slow planner call streaming at 3 tok/s"* → the planner
  calls are 2–3s; the 65s window between their usage rows is the tool
  execution, dominated by the metering stalls.
- ✅ *"the connector contributes ~26ms"* — confirmed exactly (13.6ms + 13.5ms
  schema_build, 67ms live query).

## Environment/operational footnotes (observed, not measured factors)

- `BOW_ENCRYPTION_KEY` is auto-generated per process when unset
  (`config.py:73-76`); a container restart produced a new key and stored LLM
  credentials became undecryptable ("Failed to decrypt credentials for
  provider 'anthropic'") until re-saved. Worth pinning in any persistent dev
  environment.
- The dev server runs uvicorn `--reload` (watchfiles); SQLite WAL churn logs
  "change detected" every ~30s. Reloads only trigger on `*.py`, so this is log
  noise, not a latency factor.

## Candidate directions (NOT implemented — for discussion)

1. **Make metering non-blocking for the query path**: fire-and-forget /
   queue the quota writes (they're already best-effort), or check quota from a
   cached read and reconcile asynchronously — never hold the sandbox thread.
2. **Short lock budget for bookkeeping writes on SQLite**: a per-connection
   `busy_timeout` override (e.g. 250ms) for best-effort writers, keeping 30s
   only for correctness-critical writes.
3. **Route metering through the single-writer session** instead of a separate
   competing session (it exists precisely to serialize writers on SQLite).
4. **Commit streaming block writes promptly during tool execution** so no WAL
   writer transaction spans the 60s execution window.
5. Prompt-budget review for post-observation planner calls (~29k tokens) —
   the dominant cost once the stall is gone.

## Reproduction

```bash
# Stack: docs/feedback-loops/assets/prometheus-stack/ + boot_stack.sh (sqlite default)
# 1. Run any create_data prompt against the Prometheus source, then:
sqlite3 backend/db/agent.db "select sub_timings_json from tool_executions order by started_at desc limit 1"
#    → stages.executing_code ≈ 60,19x ms; queries[0].query_ms ≈ 30,07x ms — every time.
# 2. py-spy dump -p <backend-worker-pid> during the stall
#    → bow_code_exec_* thread in _consume_query_quota / _consume_data_bytes_quota.
# 3. Same prompt with TEST_DATABASE_URL=postgresql://… → executing_code ≈ 150 ms.
```
