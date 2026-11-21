# Feedback loop — Stop must kill the running data query (create_data / inspect_data)

Pressing **Stop** while `create_data` or `inspect_data` was executing marked the
run "Generation stopped" in the UI, but the warehouse query kept running to
completion. Then the tool reported **success** on an empty result. This loop
reproduces that against a real Postgres warehouse through the real chat UI,
watching `pg_stat_activity` second by second after Stop.

## Root cause on the baseline

Baseline commit: `8bdb6bb156249deca44a803e8172bec64a669016`.

- Stop (`POST /api/completions/{id}/sigkill`) only sets the run's
  `sigkill_event` (`backend/app/ai/agent_v2.py` `_handle_completion_update`).
- `StreamingCodeExecutor.generate_and_execute_stream_v2`
  (`backend/app/ai/code_execution/code_execution.py`) checked that flag only
  *between* stages. While the query ran, it awaited `execute_code_async` with no
  watch on the flag.
- The kill path already existed. Cancelling the `execute_code_async` task sets
  the sandbox `cancel_event`, kills the child and calls
  `QueryCapturingClientWrapper.cancel_active_query` →
  `query_cancellation.cancel_thread`, which sends `pg_cancel_backend` /
  `KILL QUERY` on a side connection. Nothing on the Stop path cancelled that
  task, though. Only the tool runner's idle/hard timeouts did.
- After the query finished, the "stopped" branch emitted `df=pd.DataFrame()`,
  which create_data/inspect_data treated as a successful, empty run.

## Fix

- `_await_with_progress_heartbeat` now races the in-flight codegen/query task
  against `sigkill_event`. When Stop wins it cancels the task. That triggers the
  existing sandbox-kill and source-cancel path, and the stream ends with
  `done.stopped = True`.
- `create_data`, `inspect_data` and `write_csv` turn `stopped` into a
  `tool.end` with `observation.stopped = True`. The agent shows it as stopped
  and saves no step or CSV for the aborted query.
- `_cancel_orphan` logs "Query aborted by its run" for aborts, instead of the
  timeout wording.

## Replay

Postgres 16, a stub LLM (deterministic, no API key), the full stack.

```bash
# warehouse + app DB on a local Postgres
pg_ctlcluster 16 main start
su postgres -c "psql -c \"ALTER USER postgres PASSWORD 'postgres'\" -c 'CREATE DATABASE warehouse' -c 'CREATE DATABASE bow_app'"
su postgres -c "psql -d warehouse -c \"CREATE TABLE orders(id serial primary key, region text, amount numeric);
  INSERT INTO orders(region,amount) SELECT (ARRAY['EU','US','APAC'])[1+g%3], g*1.5 FROM generate_series(1,300) g;\""

export BOW_ENCRYPTION_KEY=<any fernet key> BOW_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium
TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/bow_app tools/agent/boot_stack.sh --dev
cd backend
uv run python ../tools/agent/seed_org.py
STUB_DS_FILE=/tmp/ds.json STUB_SLEEP_S=120 uv run python ../tools/agent/stop_query_stub_llm.py &
uv run python ../tools/agent/setup_stub_llm.py
# create a postgresql data source (host 127.0.0.1, db warehouse, sslmode disable),
# activate public.orders via PUT /api/data_sources/{id}/update_tables_status, then:
echo '{"id":"<ds id>","table":"public.orders","tool":"create_data"}' > /tmp/ds.json

cd ../frontend
WATCH_S=135 node ../tools/agent/stop_query_probe.mjs after-create_data <ds id> /tmp/stop-ev
```

The probe signs in, opens a report on the source, sends a prompt and waits for
`pg_sleep` to be active on the warehouse. It then clicks the Stop button and
samples `pg_stat_activity` once a second. Finally it reads the tool status and
step statuses from the app DB. For the baseline, run the probe once with
`backend/app` stashed and the backend restarted.

## Observed

| Run | Active `pg_sleep` after Stop | Tool result recorded | Report steps |
|---|---|---|---|
| before · create_data | kept running, gone at **118 s** (natural end) | `success`, "0 rows x 0 cols" | 1 `success` (empty query shown) |
| before · inspect_data | kept running, gone at **118 s** | `success`, "Inspection finished" | — |
| after · create_data | gone at **1 s** | stopped: "Data query stopped by user" | 1 `draft` (hidden, as for any failed run) |
| after · inspect_data | gone at **1 s** | stopped: "Data inspection stopped by user" | — |

The backend log after the fix shows
`Query aborted by its run; source cancellation: cancelled (postgresql cancel)`.
Raw timelines are in [`assets/stop-kills-running-data-query/`](assets/stop-kills-running-data-query/).
A repeat run of each "after" case gave the same 1 s result.

While running (both builds):

![running](assets/stop-kills-running-data-query/running.png)

create_data, reloaded after Stop. Before: "Created Data 120.6s · Execution
succeeded · 0 rows" and an empty "Orders by region" query. After: stopped at
3.5 s and nothing is saved.

![before create_data](assets/stop-kills-running-data-query/before-create_data.png)
![after create_data](assets/stop-kills-running-data-query/after-create_data.png)

inspect_data. Before: "Inspected 120.2s". After: 3.1 s, "Stopped by user".

![before inspect_data](assets/stop-kills-running-data-query/before-inspect_data.png)
![after inspect_data](assets/stop-kills-running-data-query/after-inspect_data.png)

## Tests

`backend/tests/unit/test_stop_cancels_running_query.py` goes through the real
sandbox. Stop during a query makes the source receive a cancel and the stream
end promptly with `stopped`. Stop during codegen cancels the codegen call. With
no Stop, the result is unchanged. With `backend/app` stashed, 3 of the 4
fail: both query-cancel cases and the codegen case, because the stream hangs
until the slow work ends. Only the no-Stop guard passes.

## Not covered

- `write_csv` shares the executor and the same `stopped` handling. It is
  unit-covered but was not driven through the UI.
- `tool_executions.status` is binary (`success`/`error`), so a stopped tool is
  stored as `error` with `success=false`. That matches the existing
  `create_artifact` stop path. The live `tool.finished` event and the UI show it
  as stopped.
- Engines other than Postgres use the same `query_cancellation` path, which has
  its own tests. Only Postgres was exercised live here.
