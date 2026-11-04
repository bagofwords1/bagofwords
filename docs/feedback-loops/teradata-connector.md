# Feedback Loop — Teradata connector hardening

A review of the Teradata Vantage connector against the official `teradatasql`
driver docs, Teradata's Data Dictionary docs, and five open-source Teradata
integrations (dbt-teradata, teradatasqlalchemy, DataHub, Superset,
teradata-mcp-server). It found these problems, all fixed here:

1. **Writes got past the read-only guard.** Teradata short forms and statements
   sqlglot cannot parse (`CT`, `CV`, `REPLACE MACRO`, `COMMENT ON … AS`,
   `MODIFY USER … PASSWORD`, `MODIFY DATABASE`, `GIVE`,
   `LOCKING ROW FOR ACCESS DEL …`) fell back to a keyword list that did not know
   them, so they ran as "reads". The usual Teradata read
   `LOCKING ROW FOR ACCESS SELECT …` was refused.
2. **A timed-out query kept running on Teradata.** The client never registered
   its connection for cancellation, so the agent's timeout abandoned the query
   in BOW only, and the timing record said `not_running`.
3. **Views had no column types.** `DBC.ColumnsV` reports view columns with a
   NULL type.
4. **Errors were 80+ lines**: a Go stack trace from the driver, shown to the
   user and fed to the agent.
5. **Every query error became a bare `RuntimeError`**, because the `yield` sat
   inside the `try` in `connect()` (the bug SQL Server's client already fixed).
6. **Discovery listed tables the user cannot read** (non-X DBC views), so the
   agent hit error 3523.
7. **Thin metadata**: no lengths or precision, no character set, PERIOD / ARRAY /
   UDT types unmapped, no primary index.
8. **Prompt mistakes**: `!=` allowed, no warning that TOP cannot go with
   QUALIFY / SAMPLE / DISTINCT, a hard-coded "60s timeout", no relative-date hint.
9. **Connection settings**: no TLS mode, no CA certificate, no data encryption,
   no driver parameters; KRB5 / TDNEGO offered while a password was required.

Also from comparing with the SQL Server and Oracle clients:

- **A.** A full Teradata logon per query (no connection pool).
- **B.** "Test connection" read every column of every table.
- **C.** No fallback when a metadata query is refused.
- **D.** Common Teradata errors missing from the prompt.
- **E.** No way to pass extra driver parameters.

## Root cause (validated)

| # | Where (before) | What |
|---|---|---|
| 1 | `backend/app/ai/code_execution/sql_guard.py:121-127` | An unparsed statement got only `_keyword_check`, which lists generic verbs (`INSERT`, `CREATE`, …), not Teradata's `CT`, `CV`, `MODIFY`, `GIVE`, `COMMENT`. sqlglot parses `LOCKING … SELECT` as a `LockingStatement`, which was not in the read list. |
| 2 | `teradata_client.py:88-109` (old) | `connect()` used a raw `teradatasql` connection, never `query_cancellation.track()`. `query_cancellation._cancel` had no Teradata branch. |
| 3 | `teradata_client.py:143-155` (old) | One `DBC.ColumnsV` query; view rows have `ColumnType = NULL`. |
| 4, 5 | `teradata_client.py:101-109` (old) | `except Exception as e: raise RuntimeError(f"{e}")` around the `yield`. |
| 6 | `teradata_client.py:151-153` (old) | `DBC.ColumnsV` / `DBC.TablesV` instead of the X views. |
| 8 | `teradata_client.py:286, 294` (old) | "use `<>` (preferred) or `!=`"; "Queries have a 60s timeout" (the org default is 180s and is configurable). |
| 9 | `configs.py:1488-1493` (old) | `logmech` options `TD2, LDAP, KRB5, TDNEGO` with a required password; nothing else. |
| A | `teradata_client.py:103` (old) | `teradatasql.connect(...)` per call. |
| B | `connection_service._acount_tables_for_validation` | Only clients with `count_tables()` get the cheap test; Teradata had none. |

Driver facts used (README of `teradatasql` 20.0.0.74, checked against the real
driver in this sandbox):

- `Connection.cancel()` "must be called from a thread other than the thread which
  is blocked".
- `sslmode` defaults to `PREFER`, which uses TLS when the database offers it and
  does not verify the certificate. Only `VERIFY-CA` / `VERIFY-FULL` verify.
- `encryptdata` defaults to `false`.
- An unknown connection parameter fails before connecting: `[Error 169] Unable to
  parse JSON connection parameters` (observed).
- A failed connect returns 83 lines (observed: `[Error 444] … Failed to connect`
  followed by `at gosqldriver/teradatasql.formatError ErrorUtil.go:84 …`). The
  default connect timeout is 10s (observed).

## Loop A — deterministic reproduction (no external services)

No Teradata server runs in a sandbox (Vantage Express is a licensed VM image),
so the driver boundary is replaced by `backend/tests/mocks/fake_teradatasql.py`.
It is a DuckDB-backed fake that reproduces what the client depends on:

- the documented parameter names, and the real error text with its Go stack trace
- `DBC.TablesV` / `ColumnsV` / `IndicesV` and their X variants
- NULL view types in `ColumnsV`
- `ColumnsQVX` only when QVCI is on
- `HELP COLUMN` with the real column titles
- `LIMIT` / `ILIKE` / TOP+QUALIFY rejected with error 3706
- `cancel()`, and error 3523 for a table the user has no access to

Everything above the driver runs for real: the client, `engine_pool`,
`query_cancellation`, the timeout wrapper and the schema renderer.

```bash
cd backend
export TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db
uv run pytest tests/unit/test_teradata_client.py tests/unit/test_sql_read_only_guard.py \
              tests/unit/test_query_cancellation.py -q
```

Against the code before the fix (`git show HEAD:<file>` swapped in):

```
tests/unit/test_teradata_client.py      29 failed
tests/unit/test_sql_read_only_guard.py  17 failed (teradata), e.g.
  FAILED test_teradata_writes_are_refused[MODIFY USER analyst AS PASSWORD = secret]
  FAILED test_teradata_writes_are_refused[CT orders_copy AS (SEL * FROM orders) WITH DATA]
  FAILED test_teradata_writes_are_refused[LOCKING ROW FOR ACCESS DEL FROM orders]
  FAILED test_teradata_reads_pass[LOCKING ROW FOR ACCESS SELECT amount FROM orders]
  FAILED test_teradata_reads_pass[HELP COLUMN sales.orders.*]
old error text: "Execution failed on sql '…': [Version 20.0.0.74] [Session 1016]
  [Teradata Database] [Error 3706] …\n at gosqldriver/teradatasql.formatError …"
```

## Loop B — full app stack against the fake driver

```bash
# from the repo root
PYTHONPATH="$PWD/tools/teradata/fake_driver" FAKE_TERADATA_STATS=/tmp/td-stats.json \
BOW_ENCRYPTION_KEY=<a fixed Fernet key, so stored secrets survive restarts> \
  tools/agent/boot_stack.sh
cd backend && uv run python ../tools/agent/seed_org.py
uv run python ../tools/agent/stub_llm_teradata.py &          # deterministic LLM
uv run python ../tools/agent/setup_stub_llm.py --stub-url http://127.0.0.1:9099/v1
cp ../tools/agent/teradata_ui_flow.mjs ../frontend/.agent-tmp/ && cd ../frontend/.agent-tmp
TD_PASSWORD='Reader#2026' node teradata_ui_flow.mjs connect /tmp/shots
node teradata_ui_flow.mjs chat /tmp/shots "Show revenue by region" revenue
```

There are two legs:

- **Scripted.** `tools/agent/stub_llm_teradata.py` drives the real planner →
  codegen → sandbox → guard → client path. It fails the run if the Teradata
  guide, the relative-date hint, or the `primary_index` metadata is missing from
  the prompts.
- **Real model.** Configure one with
  `ANTHROPIC_API_KEY=… HAIKU_MODEL_ID=claude-haiku-5-5 uv run python ../tools/agent/setup_haiku_llm.py`.

Sandbox notes:

- `main.py` runs uvicorn with `reload=True` on `backend/app`. Editing app code
  during a run restarts the worker, which can hang in shutdown while a browser
  stream is open. Restart with `tools/agent/restart_backend.sh` instead.
- Set `BOW_ENCRYPTION_KEY`, or every restart makes the stored LLM and connection
  secrets undecryptable.

### Observed — connection and discovery (UI)

| Check | Result |
|---|---|
| Test via API: valid / wrong password / DNS failure | `Found 7 tables` / `[Teradata Database] [Error 8017] The UserId, Password or Account is invalid.` / `[Teradata SQL Driver] [Error 493] … Hostname lookup failed` — one line each |
| Test with `logdata` in additional parameters | fails: `Unsupported additional parameters: logdata. Allowed: …` |
| Test with sslmode `SKIP` | fails: `Unsupported SSL mode 'SKIP'. Use one of: …` |
| Requests made by a test | 2 (`SELECT 1`, one `COUNT(*)` on `DBC.TablesVX`); no column scan |
| UI connect → discovery | `Discovered 7 tables` — `hr.salaries` (no access) and the join index are absent |
| Stored catalog, view `sales.v_monthly_revenue` | `month_start DATE, region CHAR(4), revenue DECIMAL(18,2)` (QVCI off → HELP COLUMN) |
| Stored catalog, `sales.customers.name` | `VARCHAR(100) CHARACTER SET UNICODE` |

Screenshots: `media/pr/teradata-connector/01-03`.

### Observed — scripted agent (stub LLM, connection timeout 5s)

| Prompt | Result |
|---|---|
| revenue by region (`LOCKING ROW FOR ACCESS SELECT …`) | AMER 410.24, EMEA 200.50, APAC 45.00. No new logon (pooled session). |
| latest order per customer (`QUALIFY ROW_NUMBER() …`) | one row per customer, correct |
| monthly revenue from the view | 5 rows from `sales.v_monthly_revenue` |
| `DEL FROM sales.orders …` | `Security violation (unsafe_sql): Statement 'DEL' is not allowed.` `writes_seen: []` |
| `CT sales.orders_backup AS (SEL …) WITH DATA` | `Statement 'CT' is not allowed.` `writes_seen: []` (this ran before the fix) |
| full scan of `sales.slow_scan` | `Query exceeded 5s timeout …`; fake driver: `cancels: 2, active: 0` (the agent retried once; both attempts were stopped on the server) |

Screenshots: `04-08`.

### Observed — real LLM (Claude Haiku 5.5, `claude-haiku-5-5`)

The key was supplied by the user for this run. It was passed only through the
environment and is not stored anywhere in the repo.

| Question | SQL the model sent (from the fake driver's log) | Outcome |
|---|---|---|
| What is total revenue by customer region? | `SELECT c.region, SUM(o.amount) … GROUP BY c.region ORDER BY …` | correct totals (655.74 overall) |
| Show each customer's most recent order | `… QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_date DESC) = 1` | correct |
| Monthly revenue by region for the last 3 months | `… FROM sales.v_monthly_revenue WHERE month_start >= ADD_MONTHS((SELECT MAX(month_start) …), -2)` | used the typed view and Teradata date syntax |
| How many rows are in sales.slow_scan? | `SELECT COUNT(*) …` → timeout | cancelled on the server (`cancels: 2, active: 0`). The follow-up `DBC.TableSize` was refused by the table-access guard. |
| Delete the cancelled orders | only reads (`SELECT TOP 3 …`, `GROUP BY status`, …) | `writes_seen: []` |

The real model found two prompt problems the stub could not:

1. The sample queries used `sales.orders` / `order_status`. Haiku attributed
   `order_status` to the real `sales.orders` ("not `order_status` as the schema
   index suggested").
2. Nothing said the connection is read-only, so Haiku offered to run the DELETE
   "once you confirm".

Fixed: the examples now use placeholder `exdb.ex_*` tables, and the prompt
states the read-only rule. A test now requires that no discovered table name
appears in the prompt and that "read-only" does. Rerun: Haiku answers "I can't
run a DELETE … my query tools produce read-only result sets" and hands the user
the statement. It no longer mentions `order_status`.

Screenshots: `09-13`.


## The fix

- **Guard** (`sql_guard.py:85-130, 160-210`): for Teradata, the leading verb must
  be `SELECT`/`SEL`/`WITH`/`HELP`/`SHOW`/`EXPLAIN`/`(`, after any
  `LOCKING … FOR ACCESS|READ|SHARE` prefix. `WRITE`/`EXCLUSIVE` locks are
  refused, and a `LockingStatement`'s inner statement is checked as a read.
  `HELP` is allowed. Unparsed text must be a single statement. `CHARACTER SET x`
  is ignored by the keyword check, so the prompt's own error-6706 fix is allowed.
- **Pool + cancel**: `app/data_sources/teradata_dialect.py` is a minimal
  SQLAlchemy dialect, used only so the client can use `engine_pool`. Queries go
  to the driver as text. Teradata's own `teradatasqlalchemy` was not used because
  it adds telemetry query bands. `connect()` wraps the `yield` in
  `track()`, and `query_cancellation._cancel` calls `cancel()` for dialect
  `teradata`. `teradata_uri` is added to `_invalidate_engine_pool`.
- **Errors**: `short_error()` keeps the first line and drops the
  `[Version …] [Session …]` prefix. `execute_query` re-raises the driver's own
  exception type with that message. Body exceptions pass through `connect()`
  unchanged.
- **Discovery**:
  - Tables come from `TablesVX`, falling back to `TablesV`. Columns come from
    `ColumnsVX`, with full and basic fallbacks.
  - Views are typed from `ColumnsQVX` when QVCI is on, otherwise by
    `HELP COLUMN` per view within a 120s budget. A broken view is skipped.
  - Primary indexes come from `IndicesVX`. A UPI or a declared PK becomes `pks`,
    and `metadata_json.teradata` holds `kind`, `primary_index` and
    `primary_index_unique`, now rendered for the agent (`tables_schema_section`).
  - Types carry length, precision and character set. PERIOD, ARRAY and UDT are
    mapped.
  - `count_tables()` makes the connection test a single COUNT.
- **Prompt**:
  - States the connection is read-only, and uses placeholder `exdb.ex_*` example
    tables.
  - Drops `!=` and adds the TOP/QUALIFY/SAMPLE/DISTINCT rule.
  - Uses the connection's own timeout, or no number.
  - Adds errors 2616 / 6706 / 3504 / 3706 / 3523 with fixes, `LOCKING ROW FOR
    ACCESS`, `HELP COLUMN`, and a `relative_date_hint`.
- **Settings** (`configs.py` `TeradataConfig`):
  - `sslmode` dropdown, default `PREFER`, so existing connections are unchanged.
  - `ssl_ca` as PEM, sent as `sslbase64`.
  - `encryptdata`, default on.
  - `additional_params`, limited to an allowlist of real driver parameters. An
    unsupported key fails the connection test with the allowed list.
  - `logmech` limited to TD2 / LDAP. Saved KRB5/TDNEGO values still pass through.

- **Shared timeout message** (`code_execution.QueryTimeoutError`): "Use LIMIT"
  is now "Limit rows with the source's own syntax (LIMIT, or TOP n on SQL
  Server and Teradata)". LIMIT is a syntax error on both.

## What this proves / regression notes

- The unit tests above are general invariants and fail on the old code:
  - only accessible objects are listed
  - every column is typed, with or without QVCI
  - one logon serves many queries
  - a timeout cancels the query on the server
  - errors are one line and keep their type and code
  - TLS/encryption settings reach the driver
  - every allowed extra parameter is a documented driver parameter
- Pre-existing failures that reproduce with these changes stashed (196 tests,
  same set): `test_sharepoint_*` (the sandbox's `REQUESTS_CA_BUNDLE` proxy CA),
  `test_reply_language_reminder`, `test_planner_upload_origin`, and
  `test_artifact_precompile` (one of them is timing-flaky). `pyodbc` tests needed
  `apt-get install unixodbc` in this sandbox.
- Not verified against a real Teradata system. The fake follows the docs, but
  real-system checks remain:
  - the exact `HELP COLUMN` titles on a given release
  - whether `ColumnsQVX` errors or returns rows when QVCI is off
  - `cancel()` on a long real request

  Use `tests/integrations/ds_clients.py -k teradata` with `integrations.json`
  pointing at a ClearScape Analytics environment.
