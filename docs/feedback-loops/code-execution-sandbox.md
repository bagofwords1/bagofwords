# Feedback Loop — generated code runs in a sandboxed child process

Model-generated Python used to run through `exec()` inside the uvicorn API
worker, guarded only by the AST denylist. One escape had the whole product's
blast radius: the worker holds `BOW_ENCRYPTION_KEY`, the app database URL,
live connector objects for every data source, and any Kerberos keytab. There
was no memory cap and a runaway loop could not be killed.

Design and operator knobs: `docs/design/code-execution-sandbox.md`.

## Root cause (validated)

`StreamingCodeExecutor.execute_code` built a namespace with the wrapped
clients and called `exec(code, local_namespace)` on a thread of the API
process (`backend/app/ai/code_execution/code_execution.py`, formerly ~L1225).
`PptxCodeExecutor.execute_pptx_code` did the same for python-pptx scripts.

## The fix

`backend/app/ai/code_execution/sandbox/` — a spawned (not forked) child
interpreter per execution with a scrubbed environment, rlimits, no_new_privs,
a wall-clock kill, and Landlock filesystem/TCP confinement where the kernel
offers it. Data-source queries and web fetches are brokered back to the
parent over a pipe, so the `QueryCapturingClientWrapper` instances (capture,
timeouts, quotas, rate limits, SQL rendering) never leave the trusted side.
Results return as Arrow IPC; the parent never unpickles anything from the
child. `BOW_CODE_SANDBOX=inprocess` restores the old behavior for debugging.

## Loop A — unit tests (deterministic, no LLM)

```bash
cd backend
BOW_DATABASE_URL='sqlite:///db/test.db' uv run pytest -q \
  tests/unit/test_code_sandbox.py \
  tests/unit/test_swallowed_query_error.py tests/unit/test_sklearn_sandbox.py \
  tests/unit/test_query_timeout.py tests/unit/test_query_params.py \
  tests/unit/test_query_concurrency.py tests/unit/test_code_execution_heartbeat.py \
  tests/unit/test_usage_metering_buffer.py tests/unit/test_query_cancellation.py \
  tests/unit/test_db_error_hints.py tests/unit/test_concurrent_tool_dispatch.py \
  tests/e2e/test_loadables.py tests/e2e/test_report_rerun_params.py
```

Observed: all green (the pre-existing suite runs unchanged against the
sandbox; `test_code_sandbox.py` adds the boundary tests — env scrub, no
parent memory, client attribute lockdown, wall-clock kill, memory cap,
cancel kill, original-exception propagation, Arrow round trip and fallback,
pptx round trip, Landlock ABI probe layout).

`test_code_executions_overlap_without_global_lock` gained a warm-up call:
it measures overlap, and the first execution in a process now pays the
interpreter start.

## Loop B — live stack, real data source (no LLM needed)

The LLM key available in the authoring environment had no API credit, so
the chat path could not be driven. The same executor is reachable without
an LLM through saved queries (entities): `POST /api/entities` with
`generate_df` code and a data source, then `POST /api/entities/{id}/run`.

1. Boot backend (`uvicorn main:app`, pinned `BOW_ENCRYPTION_KEY`) and
   frontend (`yarn dev`); sign up through `/users/sign-up`; install the
   Chinook demo source with `POST /api/data_sources/demos/chinook`.
2. Create an entity whose code queries `Invoice ⋈ Customer` grouped by
   country and run it.

Observed, per layer:

- **HTTP**: `POST /api/entities/{id}/run` → 200 with 10 rows
  (`USA 523.06 / 91 invoices`, …); the UI "Refresh Data" button issues the
  same call and re-renders.
- **Backend log** (`app.ai.code_execution.sandbox.runner`):
  `code sandbox: child pid=6133 mode=data landlock=unavailable … spawn_ms=1.7`
  then `code sandbox: done pid=6133 rows=10 total_ms=16.5` — a different
  pid from the API worker; 1.7 ms because the warm pool had a child ready
  (a cold spawn measured ~450 ms).
- **DB**: `entities.data` holds the encrypted+zlib payload; decrypted with
  the Fernet key it contains the same 10 rows.
- **UI**: `/queries/{id}` renders the table (screenshot in the session).
- **Negative cases through the API** (all rejected, no child harmed):
  `import os` → "Forbidden import: 'os'"; `DROP TABLE` in a string →
  forbidden SQL; a query on a missing table → the pandas `DatabaseError`
  text from the trusted-side wrapper, proving original-exception
  propagation; `client.database` → "exposes only execute_query(...)
  inside the sandbox".

## What was not verified here

The authoring container's kernel has `CONFIG_SECURITY_LANDLOCK` unset, so
the Landlock path ran fail-open (logged once as a warning). The bindings
were reviewed against `linux/landlock.h` (struct layouts, ABI masks) and
their layout is unit-tested, but the syscalls themselves need a host with
the LSM enabled to be exercised. Docker's default seccomp profile has
allowed the Landlock syscalls since 20.10.
