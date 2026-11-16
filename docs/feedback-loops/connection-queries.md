# Feedback loop: connection Queries tab (running and past queries, kill, app-wide limit)

Linear BOW-130. This adds a **Queries** tab to *Manage connection*. It is visible
to connection managers only and lists every query BOW sends to the connection:
the exact SQL (or native query) that reached the source, never the generated
Python. Running and queued queries can be killed. The connection's
concurrent-query limit now holds across every worker and replica, not per
process.

## What changed

- **`query_executions` table** (`backend/app/models/query_execution.py`,
  migration `queryexec01`). One row per query. The same row is three things:
  the history entry, the slot behind the concurrency limit, and the kill
  target.
- **App-wide limit** (`backend/app/data_sources/query_executions.py`). Before a
  query starts, the wrapper locks the connection's row and counts the live
  `running` rows plus the `queued` rows ahead of it. The count and the claim
  happen in one transaction: `SELECT … FOR UPDATE` on Postgres, `BEGIN IMMEDIATE`
  on SQLite. If the connection is full, the query waits its turn, first come
  first served, up to `BOW_QUERY_SLOT_WAIT_SECONDS` (default 60, never more
  than the query timeout). It then fails with a terminal `ConnectionBusyError`.
  The per-process semaphore in `query_concurrency.py` is gone.
- **Leases.** A per-process supervisor thread renews its rows' leases every 5 s
  (lease 30 s) and delivers kill requests every second. A dead worker's rows
  stop counting once their lease lapses. A leader job stamps them `stale`, and
  a daily job purges history older than 90 days.
- **Kill.** `POST /api/connections/{id}/queries/{qid}/kill` (`manage_connection`)
  sets `kill_requested_at`. The owning worker cancels the statement at the source
  through the existing `query_cancellation` path. The query fails with a
  terminal `QueryKilledError`. The agent explains it and does not resend the
  query: no code retry and no tool retry.
- **SQLite never stalls a query on its writer lock.** The flow running a query
  can hold SQLite's single writer lock; usage metering already follows this
  rule. The slot claim waits at most 1 s for the lock. If it is still held, the
  query is admitted against this worker's own count at the same limit. Its
  history writes are queued, and the supervisor records them once the lock
  frees. Only the claim transaction takes the lock up front (`BEGIN IMMEDIATE`);
  reads and single-row updates do not. Postgres has no fallback.
- **Busy and killed queries end the code attempt, not the conversation.**
  `create_data` gives the planner retry guidance instead of publishing the raw
  error as a failed run.
- **Live wait in chat.** The tool card shows *"Waiting for a free slot on
  warehouse-pg · 2/2 running"*. The heartbeat repeats it while the query waits.
- **Attribution.** Each row records the user, report, run, tool call, code
  attempt, tool attempt, its position in the code run, the worker, and the
  source (chat, dashboard, query run, entity, …).

## Replay

The loop used Postgres 16 as both the app DB and one warehouse, SQL Server 2022
in Docker as the second warehouse, and three real uvicorn workers.

```bash
pg_ctlcluster 16 main start          # databases: bow_live (app), warehouse (public.orders)
export MSSQL_SA_PASSWORD='<pw>'     # read by seed.py and mssql_active.py
docker run -d --name mssql -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD -p 1433:1433 \
  mcr.microsoft.com/mssql/server:2022-latest     # + msodbcsql18 for the client
cd backend
BOW_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/bow_live .venv/bin/alembic upgrade head
BOW_DATABASE_URL=… BOW_ENCRYPTION_KEY=<fernet> BOW_QUERY_SLOT_WAIT_SECONDS=150 \
  .venv/bin/uvicorn main:app --port 8000 --workers 3 &
(cd ../frontend && yarn dev &)
.venv/bin/python ../tools/agent/seed_org.py
.venv/bin/python ../tools/agent/connection_queries/seed.py "$EVIDENCE_DIR/live.json"
STUB_DS_FILE=stub_ds.json .venv/bin/python ../tools/agent/stop_query_stub_llm.py &   # see below
.venv/bin/python ../tools/agent/setup_stub_llm.py
cd ../frontend
node ../tools/agent/connection_queries/04_pg_limit_kill.mjs       # Postgres, limit 2
node ../tools/agent/connection_queries/05_mssql_busy_kill.mjs     # SQL Server, limit 1, wait 20 s
node ../tools/agent/connection_queries/06_permissions_bypass.mjs
node ../tools/agent/connection_queries/07_worker_crash.mjs
```

`stub_ds.json` maps agent names to their ids, for example
`{"agents": {"warehouse": {"id": …, "table": "public.orders"}, "sales": {"id": …, "table": "dbo.orders"}}}`.
The stub runs exactly the `SQL: …` statement in each chat message. The
requested model was Claude Haiku 5.5. Both available Anthropic keys returned
*"credit balance is too low"*, so the live chats ran on the deterministic stub.
The feature lives below the model, in the query path, so the model does not
change what is verified here.

## Observed

| Check | Result |
|---|---|
| 4 concurrent chats from 2 users on 3 workers, limit 2, `pg_stat_activity` sampled every 500 ms (342 samples) | **Peak 2 active** `pg_sleep` on Postgres. 2 ran and 2 were queued, served first come first served. |
| Manager kills a running Postgres query from the tab | Gone from `pg_stat_activity` in **2.1 s** and **1.9 s** (two runs). `cancel_outcome = cancelled (postgresql cancel)`. The oldest queued query took the slot. |
| Manager kills a queued query | Stops waiting at once. The row is `cancelled` with no start time. |
| SQL Server, limit 1, `sys.dm_exec_requests` sampled (116 samples) | **Peak 1.** Killed with `KILL`: cancelled after 1.0 s, gone from SQL Server 0.7 s after the second kill. |
| Busy (slot wait 20 s) | Row `busy` with a 20 s wait. The agent replies *"The connection is busy right now…"*; the run does not error. |
| Killed query's chat | The tool card shows the cancellation. The agent replies *"Your query was cancelled by an admin of the connection"*; no red error and no retry. |
| Member (no `manage_connection`) | No *Manage connection* entry. List and history API **403**. Killing their own query **403**, also through another connection's URL. |
| Manager with the wrong connection id or an unknown id | **404**. A finished query: **409** `connection.query_finished`. The target kept running through every refused attempt. |
| `kill -9` the worker holding a slot | Slot freed after **30.2 s** (the lease). Rows read `stale`, *"The worker running this query stopped responding"*. uvicorn respawned the worker. |
| Hebrew | Tab, table and reasons translated; layout mirrored; SQL kept LTR. |

Screenshots, the flow GIF and the raw samples are in
[`media/pr/connection-queries/`](../../media/pr/connection-queries/).

## Tests

- `backend/tests/unit/test_query_concurrency.py` (21 tests) covers:
  - rows held by other workers counting against the limit
  - a dead worker's lapsed leases not counting
  - FIFO ordering of a burst
  - **two real processes** sharing one limit, checked by peak overlap of the recorded run intervals
  - kill while queued and kill while running (the source receives the cancel)
  - history rows carrying the rendered SQL
  - timeouts and stopped runs
  - lease renewal, the stale sweep and the purge

  Passes on SQLite and Postgres.
- `backend/tests/e2e/rbac/test_connection_queries.py` (4 tests) covers
  manager-only access, kill through the API, the id-from-another-connection
  case, and history filters and paging. Passes on SQLite and Postgres.
- `test_query_concurrency.py` also covers a SQLite database held locked by
  another writer: the query starts in under 3 s, the per-process limit still
  holds, and the history rows appear once the lock frees. It also covers a
  connection deleted under a running client, whose query runs untracked.
  `test_usage_metering_buffer.py`'s writer-lock stall test passes again.
- `backend/tests/unit/test_query_failure_observation.py`: killed and busy
  queries end the attempt, not the conversation. Fails without the fix.
- `backend/tests/unit/test_code_execution_heartbeat.py`: the wait is streamed
  and repeated on heartbeats.

## Known limits

- **A crashed worker can leave its statement running at the source.** The slot
  frees after the lease, but the source does not know BOW's process died.
  Postgres keeps a `pg_sleep` going until it ends; this was seen live. Closing
  that gap needs the source session id (backend pid / SPID) stored on the row
  and cancelled from the sweep. That is a follow-up.
- **Clients that enforce their own deadline** (`_manages_query_deadline`, for
  example the BOW source client) are limited and recorded, but a kill cannot
  interrupt the call in flight. It ends when the client returns.
- **On SQLite, while another writer holds the lock**, the limit is kept per
  worker instead of app-wide, and the queued rows are not visible (or killable
  from another worker) until they are written. The API still signals the
  owning worker directly.
- **Queries that never go through the wrapper** (connection tests, indexing) are
  neither limited nor recorded, as before.
