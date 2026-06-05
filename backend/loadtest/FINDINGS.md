# Reliability & Performance Findings

Investigation triggered by "network error under parallel prompts." Sandbox:
4 vCPU / 15 GB, Postgres 16, uvicorn **2 workers** (prod-style), Anthropic Haiku,
chinook demo. Methodology: `loadtest/` harness (concurrent SSE) + `sample_metrics.sh`
(CPU/mem/PG pool) + `pg_stat_statements` per-query profiling.

---

## 1. Root cause of "network error": DB connection-pool exhaustion (not CPU)
At ~30–50 concurrent completions the per-worker SQLAlchemy pool (20+20=40)
exhausted → `QueuePool limit … timeout 30.00` → 500s and agent-internal failures
that truncate the SSE stream → browser renders **"network error."** CPU peaked
~60% the whole time; Postgres never refused — it's an **app-side pool** ceiling.

**Baseline:** L10 100%, L30 96.7%, **L50 hard-failed (0%)**; 107 QueuePool timeouts;
idle-in-transaction peaked 62.

## 2. Fixes shipped (in order found)

| # | Fix | Effect |
|---|---|---|
| Pool | Release the agent's DB connection between steps (`_release_db_between_steps`) + per-worker agent semaphore (`BOW_MAX_CONCURRENT_AGENTS`) | connection no longer held idle-in-transaction during LLM/tool calls |
| Streaming | `_handle_streaming_event`: skip the session for non-writing `tool.progress` events; bound background sessions (`BOW_MAX_BG_DB_SESSIONS`) | streaming-event QueuePool timeouts 30→9 |
| Scoring | Early instructions/context scoring once per completion (was every planner iteration) | removed N−1 redundant Judge LLM calls + sessions |
| **Avalanche** | **`DataSource.reports` selectin → lazy** | **the big one — see §3** |
| Read path | `Organization.settings` selectin→joined (org_settings 7/req→1/req) + Tier-A to-one joins | fewer round-trips on every page load |
| Context O(growth) | code-reuse + message-history bounding (see §4) | the "slows-down-over-time" retrievals |

## 3. The dominant bug: `DataSource.reports` selectin avalanche
`report.data_sources`(selectin) → `DataSource.reports`(selectin) loaded **every
report sharing the data source**, cascading into all their widgets→steps. A
single `session.get(Report)` transitively loaded the org's whole steps table:
`SELECT steps WHERE widget_id IN (…)` was **98.4% of all DB time** and grew
**O(reports per data source)** — a true "slows down as data grows" bug.

**Fix:** `DataSource.reports` → `lazy="select"` (nothing reads it). Result on the
aged DB (~490 reports), identical concurrency-10 wave:

| | before | after |
|---|---|---|
| total DB time / wave | 110,364 ms | **874 ms** (126× less) |
| steps query rows/call | 406 | **1** |
| completion duration (mean) | 78.5 s | **26.9 s** |

End-to-end: **L50 70% → 100%**, wall 342 s → 133 s, QueuePool timeouts 79 → 21.

## 4. Agent-flow DB profiling (`pg_stat_statements`, 1 completion = ~2,300 queries)
Most queries are cheap but the count + a few O(growth) scans matter at scale and
under concurrency (2,300 pool checkouts/completion).

**O(growth) retrievals fixed (would degrade as data accumulates):**
- **`code_context_builder`** (3 methods) — fetched *every* step that used the
  query's tables + full `Step.code` blob (501-row query), ranked in Python, kept
  2. → SQL `ORDER BY last_used_at DESC LIMIT 50`, fetch code only for top_k
  (`_attach_code`). **steps rows/call 502 → 50.**
- **`message_context_builder`** — fetched *all* completions for the report then
  `[-max_messages:]` in Python (O(conversation length), every iteration). →
  `ORDER BY created_at DESC LIMIT max_messages+1`.

**Constant-but-wasteful, fixed:**
- `User.{api_keys, user_data_source_credentials, user_connection_credentials}`
  selectin → lazy (only read via explicit `select()`; never serialized). ~342
  queries/completion removed. (`external_user_mappings` kept selectin — serialized
  in `UserSchema`.)
- `message_context_builder` step/widget title: loaded full `Step` (code+data
  blobs) for `.title` → `select(Step.title)`.
- `DataSource.entities` selectin → lazy (never read via relationship).

**Deferred (Tier-B risk — not done):**
- Lightweight agent re-fetches via `raiseload`/`load_only` in
  `_handle_streaming_event`/`_handle_tool_output`: the heavy graphs they pull
  (`Step.created_entity` ~173×, `external_user_mappings` ~124×) are used in
  serialization, so blanket lazy/raiseload risks `MissingGreenlet` across
  hard-to-enumerate paths. These are **constant per completion (not O(growth))**,
  so lower priority. A safe version is a slow `lazy="raise"`-in-CI migration.

## 5. Resource sizing read
- **Not CPU/RAM bound** at any point (CPU ≤ ~70%, mem ≤ ~5 GB on 4 vCPU).
- The ceiling is **concurrent long-lived agent runs vs. pool capacity**. Keep
  `BOW_MAX_CONCURRENT_AGENTS × (~1–2 conns) + BOW_MAX_BG_DB_SESSIONS` under
  `pool_size + max_overflow` per worker.

## 6. Remaining recommendations (not implemented)
- Set `idle_in_transaction_session_timeout` on the prod pool path (leak safety net).
- SSE heartbeat (`: ping`) so long silent gaps aren't reaped by proxies.
- Autovacuum tuning for never-vacuumed tables (`context_snapshots`,
  `instruction_usage_events`, `llm_usage_records`).
- The Tier-B agent re-fetch trimming, via a test-backed `lazy="raise"` migration.

## Reproduce
```bash
cd backend && source .venv/bin/activate && source .sandbox_env
bash loadtest/run.sh "10,30,50" "show list of albums"   # results_*.json + metrics_*.csv
# per-query profiling: SELECT pg_stat_statements_reset(); run; inspect pg_stat_statements
```
