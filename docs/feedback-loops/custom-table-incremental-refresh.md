# Feedback Loop — "every refresh is doing all of the data from scratch"

A custom table re-extracted its whole query on every refresh: one long query
on the source, holding the connection's refresh lock so every sibling table
waited, with no record of what earlier refreshes had done. The claim validated
here: a table can load once in resumable chunks, then refresh by reading only
rows at or after a stored checkpoint — and **still hold exactly what a full
refresh would** — on SQL sources (Postgres, SQL Server) and on a connector
that is not SQL at all (Aria Operations), with every run recorded.

Design and decisions: `docs/design/custom-tables-incremental-refresh.md`.

## Root cause (validated)

Before this change (`8b104abc~1`):

- `backend/app/services/custom_query_service.py` `refresh()` called
  `extractor.extract_to_artifact(client, cq.definition_sql, …)` unconditionally:
  the whole query, into a new file, every time.
- The whole run sat inside `artifacts.refresh_lock(connection.id)`, one lock
  per connection, so a long extraction blocked every other table on it.
- `ConnectionTable` kept only `last_refresh_status / _error / _ms` — the last
  run overwrote the one before.
- No field existed to hold a checkpoint, cursor column or key.

Three defects in the existing path were found by running messy data through
it, and are fixed here because incremental refresh would have inherited them:

| Defect | Where | Symptom |
|---|---|---|
| Column types were taken from the first 50,000-row batch and never revisited | `extractor.extract_to_artifact` (first batch `CREATE TABLE AS`, later `INSERT … SELECT *`) | A text column empty in the first batch became `INTEGER`; the first later value failed the whole extraction. A decimal that needed more digits than the first batch showed overflowed. |
| Arrow batches were built through a dict keyed on column name | `sources.SqlAlchemySource.stream_batches` | `SELECT a.id, b.id` silently kept one of the two columns. |
| SQL Server's plan prices each `(MAX)` column at a flat ~4 KB | `sql_dialect.explain_mssql` | A 565k-row table with two short `NVARCHAR(MAX)` columns was "estimated" at 4.4 GB and refused. |

## Environment (fresh sandbox)

```sh
cd backend && uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db"; mkdir -p db
```

Loop B needs Docker (in cloud sandboxes the daemon is not started:
`dockerd > /tmp/dockerd.log 2>&1 &`) and, for SQL Server, Microsoft's ODBC
driver on the host (`msodbcsql18` from packages.microsoft.com):

```sh
docker run -d --name bow-pg -e POSTGRES_PASSWORD=pw -p 55432:5432 postgres:16
docker run -d --name bow-mssql -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD="$MSSQL_PW" \
  -p 51433:1433 mcr.microsoft.com/mssql/server:2022-latest
docker run -d --name bow-maria -e MARIADB_ROOT_PASSWORD=pw -e MARIADB_DATABASE=shop -p 53306:3306 mariadb:11
(cd tools/aria_operations && uv run --project ../../backend uvicorn mock_suite_api:app --port 8443 &)

export INCR_LOOP_PG_URL=postgresql://postgres:pw@localhost:55432/postgres
export INCR_LOOP_MSSQL_URL="mssql+pyodbc://sa:$MSSQL_PW@localhost:51433/master?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes"
export INCR_LOOP_MARIADB_URL=mysql+pymysql://root:pw@localhost:53306/shop
export INCR_LOOP_ARIA_URL=http://127.0.0.1:8443
(cd backend && uv run python ../tools/agent/fake_servicenow.py &)
export INCR_LOOP_SERVICENOW_URL=http://127.0.0.1:9311
```

Throwaway local containers only. Passwords and keys come from the
environment; none are in this repo. Loop B reseeds and modifies `shop`.

## The data (deliberately messy)

`tools/agent/custom_table_incremental_seed.py` builds the same `shop` dataset
in both engines — 564,706 orders and 150,277 events at the default size:

- ids with gaps (every 17th missing); two years of history with most rows in
  the last months
- `updated_at` missing on 2,676 rows, identical on ~6,000 (one bulk import),
  `1970-01-01` on five, and `2099-01-01` on one
- a `notes` column empty for the first sixth of the table; amounts negative,
  NULL and twelve digits wide; currency codes in mixed case with stray spaces
- names with quotes, emoji, CJK and right-to-left text; `''` and NULL both
- JSON that is an object, an array, a bare string or missing, row to row
  (JSONB on Postgres, `NVARCHAR(MAX)` on SQL Server)
- SQL Server: `datetime2(7)` values finer than a microsecond, a `rowversion`
- an events table with no key and exact duplicate rows

## Loop A — deterministic (no external services)

```sh
cd backend && uv run python ../tools/agent/custom_table_incremental_loop.py
```

Observed:

```text
=== Loop A — deterministic ===
506 passed in 56.36s
```

`tests/unit/test_incremental_core.py` (typed checkpoints, windows, per-dialect
rendering, normalization, the loader, JSON-request sources),
`tests/unit/test_incremental_refresh.py` (the real service, real SQLite
source, real encrypted DuckDB files and locks), the pre-existing
`tests/unit/test_fast_artifact_lifecycle.py` unchanged, and the API tests in
`tests/e2e/rbac/test_custom_table_incremental_api.py`. The same suites pass
with Postgres as the app database (`--db=external`, 81 passed).

Invariants they hold, each over generated or parametrized inputs rather than
one scenario:

- after any sequence of inserts, updates and re-reads, an incrementally
  refreshed table equals the source (merge and append, wrapped and
  placeholder queries, several lookbacks and chunk sizes)
- an incremental run reads only what is at or after the checkpoint
- rows sharing the checkpoint value are neither lost nor doubled; rows with no
  cursor value are carried, not dropped
- a first load's windows cover the source exactly once and follow where the
  rows are, not how wide the cursor's range is
- a load that fails or runs out of budget part-way resumes from its last
  finished window; a deleted table leaves no file behind
- a failed run of any kind leaves the served artifact *and* the checkpoint
  exactly as they were
- every refresh that starts leaves exactly one finished run; recording a run
  never breaks a refresh
- lineage columns never reach a query or the stored column list
- a window whose rows were committed but not recorded is not loaded twice;
  a renamed table keeps its rows; a rebuild that keeps failing does not stop
  a served table from picking up what is new

## Loop B — live engines

Same command with the `INCR_LOOP_*` variables set. Observed:

```text
=== Loop B — live (INCR_LOOP_PG_URL, INCR_LOOP_MSSQL_URL, INCR_LOOP_MARIADB_URL, INCR_LOOP_ARIA_URL, INCR_LOOP_SERVICENOW_URL) ===
18 passed, 2 skipped in 146.31s
```

(The two skips are SQL Server-only tests on the Postgres leg.)

| Scenario (`tests/integrations/test_custom_table_incremental_live.py`) | Postgres 16 | SQL Server 2022 |
|---|---|---|
| First load, 564,706 rows, 9 windows; copy equals source by aggregate and by a 2,700-row sample | pass | pass |
| The 2099 row does not move the checkpoint into the future | pass | pass |
| Increment after 941 inserts, 516 updates, a late arrival, a hard delete, an edit to a row with no cursor | pass | pass |
| The windowed query uses the `updated_at` index (`EXPLAIN` / `SHOWPLAN_XML`) | pass | pass |
| Late change older than the lookback and the deleted rows are corrected by a full refresh | pass | pass |
| First load interrupted on its third window, resumed, equals source | pass | pass |
| `{{since}}` / `{{until}}` placed by the admin, rendered in the engine's own literals | pass | pass |
| Append on a keyless table with exact duplicates; reading a window twice changes nothing | pass | pass |
| Daily rollup recomputes whole days only | pass | pass |
| JSON of every shape, UUIDs, wide decimals, emoji / CJK / RTL names, `''` vs NULL | pass | pass |
| `rowversion` as a numeric cursor (catches a change that sets no timestamp) | — | pass |
| `datetime2(7)` rows between two microseconds are not lost at the boundary | — | pass |

MariaDB 11 (a 5,000-row table the test builds, with the same hazards): the
load is cut into windows the server accepts in both query forms, the copy
follows inserts and updates, and the windowed query uses the index.

Aria Operations (mock Suite API): 123,600 rolled-up metric points across 50
series loaded by self-splitting requests (one response holds 50,000 rows);
every complete day matches the API asked directly; the next refresh asked for
one hour.

### Evidence KPI

| | Postgres | SQL Server | Aria Operations (mock) |
|---|---|---|---|
| Rows in table | 564,706 | 564,706 | 123,600 |
| First load | 11.9 s | 7.9 s | 3.7 s |
| **Incremental refresh** | **0.54 s** | **0.97 s** | **0.24 s** |
| Rows read by that refresh | 4,131 | 4,131 | 250 |
| Share of the table read | 0.7 % | 0.7 % | 0.2 % |
| Resume after interruption (rows re-read) | 433,357 of 564,706 | 433,357 of 564,706 | — |

Query sent for the incremental run (Postgres; SQL Server differs only in
`[updated_at] >= CAST('…' AS datetime2)`):

```sql
SELECT * FROM (SELECT * FROM shop.orders) bow_sub
WHERE ("updated_at" >= TIMESTAMP '2026-11-10 17:20:54.989854') OR "updated_at" IS NULL
```

Local cost of copying the served file forward before an increment (random,
poorly compressible rows; object-storage upload and download were **not**
measured — this sandbox has no object store):

| Artifact | Copy forward | Merge 10k rows |
|---|---|---|
| 0.11 GiB | 0.8 s | 0.05 s |
| 0.44 GiB | 3.3 s | 0.06 s |
| 0.88 GiB | 6.7 s | 0.10 s |
| 1.77 GiB | 12.5 s | 0.16 s |

About 7 s per GiB and linear, so at the current 2 GiB cap an increment costs
well under a minute locally. The 5 and 10 GiB points and the object-store legs
from the design doc's decision remain to be measured before raising the cap.

## Every connector

Caching and incremental refresh are not a list of blessed types. Of the 95
connector types in the registry, the 75 whose client can run a query can be
cached (`custom_query_service.is_accelerable_type`); the other 20 are file,
mail and tool connectors with no query to cache. Every cached table can
refresh incrementally. What differs per connector is only how a refresh window
reaches the source:

| How the window is expressed | Connectors | Checked by |
|---|---|---|
| SQL literals in the connector's dialect, wrapped around the query or placed with `{{since}}` | Postgres, SQL Server, MariaDB/MySQL, SQLite; Oracle, Snowflake, BigQuery, ClickHouse, Trino, Fabric, Redshift, Athena, Databricks; PostHog (HogQL), Sybase | live for the first four; rendering unit-tested for the rest |
| Added to the query's own WHERE clause (SOQL has no subquery to wrap) | Salesforce | the real service against a fake org that evaluates the conditions and pages like the query API |
| `FILTER` around the DAX table expression | Power BI | text only: the rendered DAX parses back to the expected expression |
| Fields of a JSON request | Aria Operations | live against the mock Suite API |
| Not at all: the connector's query runs whole and the window is taken from the result | every other queryable connector (ServiceNow, MongoDB, Elasticsearch, …) | the real ServiceNow client against the repo's fake instance, through the service and through the API |

`tests/unit/test_incremental_connectors.py` (31 tests). What they establish:

- **Salesforce** is read in the API's own pages to the end — 12,500 records
  where the agent-facing call stops at 10,000 — and then refreshed from its
  checkpoint with one bounded SOQL query. Dates arrive from Salesforce as
  text; they are stored as timestamps, without which a checkpoint could not be
  written back into SOQL (found by this test, not by reading).
- **A connector that cannot filter** is cached through `execute_query` and
  keeps what it has seen: a row that leaves the source's result stays in the
  table, a changed row is updated, a new one is added. Because such a source
  only ever returns what it has now, its table is never rebuilt on a schedule
  — only when the admin asks — and the run log and the modal say that each
  refresh runs the whole query.
- Through this path a table holds what the connector's own query call
  returns, including that call's row limit (ServiceNow: 100 rows unless the
  request sets `limit`, 10,000 at most). The run log says so.

## Loop C — full stack, real browser, real model

```sh
tools/agent/boot_stack.sh --dev
cd backend
uv run python ../tools/agent/seed_org.py
ANTHROPIC_API_KEY=… uv run python ../tools/agent/setup_haiku_llm.py
uv run python ../tools/agent/custom_table_incremental_seed.py
uv run python ../tools/agent/custom_table_incremental_setup.py     # prints the data source ids
uv run python ../tools/agent/custom_table_incremental_chat.py
cd ../frontend && node ../tools/agent/custom_table_incremental_ui.mjs <data_source_id> /tmp/shots
```

Observed:

- **API** (`custom_table_incremental_setup.py`): both connections created,
  `orders_cache` created as an incremental table and loaded, 564,706 rows in
  5.1 s (Postgres) and 6.1 s (SQL Server).
- **Scheduler**: with the interval set to one minute, three runs with trigger
  `schedule`, mode `incremental`, ~300 ms each, appeared in the history.
- **Browser** (`custom_table_incremental_ui.mjs`): 18/18 checks on Postgres,
  on SQL Server, and with the Hebrew locale — settings read back correctly,
  the first load shows its chunks and log, *Refresh now* adds an incremental
  run with the query it sent, a new table cannot be saved until its cursor
  (and, for merge, its key) is chosen.
- **Agent with a real model** (`custom_table_incremental_chat.py`, Claude
  Haiku): rows are changed at the source, the table refreshed incrementally,
  and the agent asked for a count that exists only in the rows that refresh
  brought in. Twice per engine, so the answer has to move:

```text
PASS  postgres round 1: source=63 refresh=incremental read 2739 rows in 332 ms; agent: "**63** … in the orders_cache table."
PASS  postgres round 2: source=203 refresh=incremental read 2878 rows in 289 ms; agent: "**203 orders** have the status …"
PASS  mssql round 1: source=63 refresh=incremental read 2739 rows in 362 ms; agent: "**63** orders have the status …"
PASS  mssql round 2: source=203 refresh=incremental read 2878 rows in 340 ms; agent: "**203** orders have the status …"
```

Screenshots (seeded sandbox data only) are in
`media/pr/claude-eager-goodall-04zi0n/`: `before-02-settings.png` vs
`after-02-settings.png`, the Runs tab for a first load and for an increment,
a new table's settings, SQL Server, and Hebrew.

## The fix

- `backend/app/data_sources/fast/incremental.py` — the connector-agnostic
  core: typed checkpoints, windows, lookback, chunk planning by row density,
  and the one connector-specific step, writing a window in the source's own
  query language (SQL literals per dialect, or the fields of a JSON request).
- `backend/app/data_sources/fast/loader.py` — writes an artifact whole, one
  window at a time, or as an increment (copy the served file forward, stage
  the window, merge by key or replace the tail); widens column types between
  batches; stamps each row with the run that wrote it.
- `backend/app/data_sources/fast/normalize.py` — one shape for every source's
  rows (column names, values Arrow cannot type, wide decimals).
- `backend/app/data_sources/fast/sources.py` — `RequestSource` for connectors
  whose query is a JSON request with a time window; splits on the API's row
  ceiling, on rollup-bucket boundaries.
- `backend/app/services/custom_query_service.py` — `refresh()` decides between
  a single full load, an increment, and a windowed load; holds the table lock
  for the run and the connection lock only per unit of work; publishes the
  artifact and the checkpoint in one conditional UPDATE.
- `backend/app/services/custom_table_runs.py`, `models/custom_table_run.py`,
  migration `cqruns01` — run history; `refresh_mode / refresh_config /
  refresh_state` on `connection_tables`.
- `backend/app/routes/connection.py` — settings on create/update,
  `POST …/refresh?full=true`, `GET …/runs`, `GET …/runs/{id}`.
- `frontend/components/datasources/CustomQueryModal.vue` — what each refresh
  fetches, the Runs tab, Full refresh.

## Independent review

After the loops were green, the backend change was reviewed adversarially
against its stated invariants (never modify a served file; a failure leaves
artifact and checkpoint intact; an incremental table equals a full refresh;
no injection through cursor values; lineage columns never reach a query). It
confirmed the window planner, the merge SQL, literal rendering, the atomic
swap, thread/ORM separation and the authorization of the new endpoints, and
found these, each now fixed with a regression test:

| Found | Fix |
|---|---|
| Renaming an incremental table broke its next refresh, and a rename during a paused load lost the windows already loaded | The stored table is renamed to match (`loader.ArtifactWriter.adopt_relation`) |
| A window committed to the part file but not yet recorded could be loaded twice on resume — duplicates in append mode | The window is recorded inside the part file in the same transaction as its rows |
| A rebuild that kept failing was retried on every tick and blocked increments indefinitely; *Full refresh* could not clear it | Three failures without progress set the rebuild aside for a day while increments continue; *Full refresh* restarts a load |
| A delta column with no values retyped a stored BOOLEAN / small integer column | Columns with no value in the delta are not used to retype |
| A column dropped at the source was silently kept with stale values | A missing stored column rebuilds the table |
| The lineage columns were reachable through the attached file's catalog name | That name is refused in queries on accelerated data |
| A cancellation between windows left the table and the run "running" | The cancellation is recorded |
| On replicas without a shared volume, any replica picking up a resume restarted the load | A replica without the part file hands the turn back to the one that has it, for 45 minutes after its last progress |
| A file renamed but not uploaded was orphaned until the sweep; a table named `bow__staging` could never refresh; a deleted table's lock file was removed while held | Cleaned up / reserved prefix / removed only when free |

One finding is left as a known limit: `grain` truncates a time-zone-aware
checkpoint in UTC, so a rollup bucketed in another zone should use a naive
timestamp or a date cursor.

## What this proves / regression notes

- Existing tables are untouched until an admin opts in: `refresh_mode`
  defaults to `full`, and the pre-existing lifecycle suite passes unmodified.
- Two bugs were caught only by the live engines and now have unit tests:
  year 1 rendered as `1-01-01` (read as 2001 by Postgres, silently dropping
  rows dated before it), and SQL Server's `(MAX)` width guess.
- One design flaw was caught only by looking at a real run in the UI: cutting
  the cursor's range evenly put the whole table in one chunk because of the
  1970 and 2099 rows. Chunks are now planned by counting.
- **Not verified here**: the remaining SQL dialects render from the same table
  and are unit-tested but have not met a live server (Oracle, Snowflake,
  BigQuery, ClickHouse, Trino, HogQL, Sybase; MySQL shares MariaDB's dialect,
  which has). Salesforce ran against a fake org and Power BI's window only as
  text — neither has met a real tenant. Aria Operations and ServiceNow ran
  against the repo's mocks. Object-storage upload/download timings and
  artifacts above 2 GiB are unmeasured.
- Connectors on the generic path are bounded by their own query call's row
  limit; giving one a paged source of its own (as Salesforce now has) is what
  lifts that.
- Pre-existing and unrelated: `tests/unit/test_sharepoint_*_client.py` fail in
  cloud sandboxes because the session's proxy CA bundle is injected into
  `requests` (`verify` is a path, the tests expect `True`). They fail the same
  way with this change stashed.
