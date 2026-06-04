# Load Test Findings — Completion SSE Concurrency

**Env:** sandbox, 4 vCPU / 15 GB, Postgres 16, uvicorn **2 workers** (prod-style,
no reload), Anthropic Haiku (`claude-haiku-4-5-20251001`), chinook demo,
prompt = "show list of albums". Pool = `pool_size=20 + max_overflow=20` per worker.

## Results

| Concurrency | Success | p50 total | p95 total | TTFB p95 | Notes |
|---|---|---|---|---|---|
| 1 (solo) | 100% | 13.3s | — | 0.29s | baseline |
| 10 | 100% | **52.7s** | 54.6s | 0.94s | 4× latency, no errors |
| 30 | 96.7% | **131.6s** | 147.6s | 9.2s | pool timeouts begin, 1 failure |
| 50 | **hard fail** | — | — | — | `POST /api/reports` → **HTTP 500** before streaming |

## Root cause: app-side DB connection-pool exhaustion (NOT CPU, NOT Postgres)

- **107× `sqlalchemy.exc.TimeoutError: QueuePool limit of size 20 overflow 20
  reached, connection timed out, timeout 30.00`** in the server log.
- **Postgres never refused** a connection (0 "too many clients"). The ceiling is
  the per-worker SQLAlchemy pool (40), not Postgres `max_connections`.
- **CPU peaked at 62%** (4 cores) — the system fails with ~40% CPU idle.
- **`idle in transaction` connections peaked at 62** (mean 33). Each in-flight
  completion holds its agent session **open in a transaction for the entire
  multi-minute run** (confirmed by the code comment at
  `app/settings/database.py:223`), occupying a pooled connection while the agent
  is actually busy on LLM / code-exec, not the DB.
- At ~30 concurrent, 2 workers × 40 = 80 pooled conns saturate (per-worker pool
  of 40 fills first) → new checkouts wait 30s → **500s on plain API calls**
  (report creation) **and** agent-internal failures (`rebuild_completion failed`,
  `Failed to score instructions/context`). Agent-internal failures truncate the
  SSE stream → browser shows **"network error"**.
- After load, connections return to the pool as plain `idle` (42 kept warm) —
  **no permanent leak**; pressure is purely concurrent-hold during runs.

## Secondary gap: latency 4× at only 10 concurrent

At concurrency 10 the pool is NOT exhausted (42/80) and CPU is ~30%, yet p50
latency goes 13s → 53s. That points to a separate bottleneck — event-loop
contention and/or sequential per-agent LLM calls — independent of the pool.
Worth a dedicated profiling pass.

## Resource-needs read

- **CPU is not the constraint** on 4 vCPU (62% peak). Adding cores won't help.
- The constraint is **concurrent long-lived agent runs vs. pool capacity**.
- Safe concurrency on this 2-worker / 40-pool config: **~10–15** completions.
  Degraded but alive at ~30; total failure by ~50.

## Recommendations (priority order)

1. **Don't hold the agent transaction open across LLM/code-exec.** Use
   short-lived sessions per DB operation (acquire → write → commit/release →
   LLM call → re-acquire). This is the highest-leverage fix; it should drop
   `idle in transaction` from ~60 toward ~0 and multiply safe concurrency.
2. **Bound concurrent agent runs with a semaphore** (per worker) so overflow
   queues with a clear "busy" signal instead of a 30s pool-timeout 500 / mid-
   stream "network error".
3. **Set `idle_in_transaction_session_timeout`** on the prod pool path (today
   it's only set on the test NullPool path) as a leak safety-net.
4. **Add an SSE heartbeat** (`: ping`) so long silent gaps don't get reaped by
   proxies/browsers (separate reliability fix from the earlier code analysis).
5. **Scale workers against Postgres `max_connections`** — with fix #1 a smaller
   pool serves far more concurrency; without it, more workers just multiply the
   idle-in-transaction pressure.

## Reproduce

```bash
cd backend && source .venv/bin/activate && source .sandbox_env
bash loadtest/run.sh "10,30,50" "show list of albums"
# results_*.json (per-level latency/outcomes) + metrics_*.csv (cpu/mem/pg pool)
```
