# Custom tables + FAST acceleration (Postgres) — design

A **custom table** is a named, materialized relation on a connection. An admin
authors SQL once in the source dialect; it runs on a schedule; agents query the
*result* locally via DuckDB instead of hitting the source. Full-table
acceleration is not a separate feature — it is `SELECT * FROM t`.

Status: design. Target branch: `claude/legacy-system-load-handling-okhqas`.

## Problem

On-prem and legacy sources (Oracle, SQL Server) — and rate-limited SaaS sources
— cannot absorb agentic query load. An agent exploring a schema issues many
small ad-hoc queries per turn, and today **every one of them goes live to the
source**. There is no result cache anywhere in the backend, and every
`execute_query` builds and disposes a fresh SQLAlchemy engine
(`postgresql_client.py:42`, `oracledb_client.py:101`), so each query also pays a
full connect/teardown.

## Principle

**Move the working set, not the query.** The admin declares what data matters as
SQL; BOW keeps the result locally in columnar form; the agent gets full SQL over
it at local-disk latency. Because the admin authors the query, the columns,
filters, and size are all explicit — which is also what makes RLS tractable in
phase 2.

## Scope

**In (v1, Postgres only):**
- Behind the `enable_custom_queries` org setting — **beta, off by default**, and
  fails closed (a settings lookup that errors leaves the feature disabled).
- Create / test / save / delete custom tables on a connection (`manage_connection`).
- Materialize each to an encrypted DuckDB artifact on a schedule (interval or daily-at-time).
- Agents query them through a DuckDB-backed client, in DuckDB SQL.
- Schema context + codegen hints so the model knows a relation is local and how fresh it is.
- Custom-query count surfaced on connection detail.
- `system_only` connections only.

**Out (v1):**
- **RLS — phase 2.** No row filtering; a custom table returns the same rows to every user of an agent that has it activated.
- Incremental / append refresh — **full reload only**.
- FAST on introspected tables (raw `ConnectionTable` rows) — v1 accelerates *queries*, not tables.
- Connectors other than Postgres.
- `user_required` connections.
- Live (non-materialized) views. A custom table is always materialized; inlining its SQL into agent-generated SQL would require rewriting generated SQL, which we are deliberately not doing.

## Data model

Custom tables live in `ConnectionTable` rather than a new model, so activation
(`DataSourceTable.is_active`), per-user overlays, `table_stats`, and schema
context rendering all keep working with no changes.

`backend/app/models/connection_table.py` gains:

```
kind                      String   'table' | 'bow'    default 'table', not null
definition_sql            Text     nullable    -- kind='bow' only, source dialect
refresh_schedule_mode     String   'interval' | 'time'
refresh_interval_minutes  Integer  nullable
refresh_at_time           String   nullable    -- "HH:MM" (24h)
last_refreshed_at         DateTime nullable
last_refresh_status       String   nullable    -- ok | error | running
last_refresh_error        Text     nullable
artifact_path             String   nullable    -- opaque uuid filename
artifact_key_enc          Text     nullable    -- per-artifact DuckDB key, Fernet-encrypted
artifact_bytes            BigInteger nullable
```

Schedule field names deliberately mirror the existing block on `Connection`
(`reindex_schedule_mode`, `reindex_interval_minutes`, `reindex_at_time` —
`models/connection.py:48-50`) so the UI and scheduler patterns carry over.

`kind='bow'` marks a BOW-managed relation. Deliberately **not** `'view'` — a
view implies non-materialized, which is the opposite of what this is.

`no_rows` (already present) holds the materialized row count. `columns` is
populated from the test run and is required — the agent's schema context, the
DuckDB schema, and phase-2 RLS predicates all depend on it. `pks` / `fks` are
stored as empty lists.

New unique constraint: `(connection_id, name)` — a custom table must not collide
with an introspected table name on the same connection.

Migration under `backend/alembic/versions/`.

### Trap to guard

`ConnectionService.refresh_schema` (`connection_service.py:1061`) upserts
`ConnectionTable` rows from live introspection. It **must exclude
`kind='bow'` rows** from its upsert and any stale-row sweep, or a scheduled
reindex will silently delete every custom table. This needs an explicit test.

## Storage & refresh

- Artifacts are **encrypted DuckDB database files**, not Parquet, at
  `uploads/fast/{connection_id}/{uuid4}.db`. See Security below — encryption is
  the sandbox boundary, not just at-rest compliance.
- Write locally to `.tmp`, then atomic rename (the QVD path already does this;
  `qvd_client.py`). On NFS, never write in place: concurrent DuckDB writes over
  NFS are unreliable, while read-only readers are fine.
- Refresh **streams**; it must never materialize the full result. See
  "Bounding extraction" below.
- Job registered on the existing APScheduler (`core/scheduler.py`), using
  `try_acquire_scheduler_leader` / `claim_scheduled_run` for multi-replica
  dedup. Add jitter so replicas don't stampede a source on the same minute.
- **One refresh at a time per connection**, via a semaphore (precedent:
  `qvd_client._get_warmup_semaphore`).
- On success: write new artifact, swap the `artifact_path`, delete the old file.
  On failure: keep serving the previous artifact, set `last_refresh_status
  = 'error'` and `last_refresh_error`. A failed refresh never blanks a
  working relation.
- On column drift between runs, update stored `columns` and log it.

## Bounding extraction

A custom table is the one place a deliberately huge scan can happen. A
`SELECT *` against a 2-billion-row table must not take the process down. Three
independent layers, all required:

**1. Refuse before running.** Postgres `EXPLAIN (FORMAT JSON)` returns
`Plan Rows` and `Plan Width` without executing; estimated bytes = rows × width.
Check at **save time** and again before each scheduled refresh, and reject over
a configured budget with the real numbers in the error. Cheap, and it catches
honest mistakes before they ever run.

**2. Never materialize.** The extractor streams — server-side cursor
(`stream_results=True` / `yield_per`) or `COPY … TO STDOUT` on Postgres —
appending batches into the DuckDB artifact. Peak memory is O(batch), independent
of result size.

> This is a **new extraction path, not a change to `execute_query`**.
> `execute_query` returns a full DataFrame by contract and every existing caller
> depends on that. Refactoring both at once is how this feature stalls.

**3. Hard caps with graceful abort.** Max rows, max bytes, and max wall-clock per
refresh. On breach: abort, keep the previous artifact, record
`last_refresh_error`. Plus a per-connection storage budget checked before write.

**The budgets are per organization.** Four settings on the AI settings page,
shown under "Custom tables" and gated by `manage_settings` like every other
setting there (`backend/app/data_sources/fast/limits.py`):

| Setting | Default | Range | Guards |
|---|---|---|---|
| `custom_table_max_rows` | 5,000,000 | 1,000 to 100,000,000 | rows in one table (estimate, mid-load, increment) |
| `custom_table_max_gb` | 2 | 1 to 64 | estimated result bytes and the artifact on disk |
| `custom_table_max_scan_gb` | 100 | 1 to 10,240 | bytes the source reads per refresh (BigQuery, Snowflake); also BigQuery's `maximum_bytes_billed` |
| `custom_table_max_requests` | 512 | 16 to 10,000 | requests per extraction on paged sources (Power BI, Analysis Services, BusinessObjects, PostHog) |

A write outside the range is refused (400) and a stored value is clamped on
read, so a setting moves a limit and never removes it. The limits are attached
to the client in `CustomQueryService._prepare_client`, the one place a client
is prepared for extraction, and read from the connection's own organization:
preview, create, manual, scheduled, resumed and incremental refreshes all pass
through it, so none can fall back to the defaults by omission. The wall-clock
limit (30 minutes per window) is not a setting.

**The Test button is bounded too.** The modal's preview wraps the admin's SQL in
a subquery with a `LIMIT`; it never runs raw unbounded SQL from a UI action.

Agent queries against the artifact need none of this — they are local DuckDB and
stream naturally. Only the extractor is affected, which is one code path rather
than every client.

## Query path

A new `FastQueryClient` (DuckDB-backed) is constructed in
`DataSourceService.construct_clients` (`data_source_service.py:2143`) for each
connection that has at least one **activated** custom table, keyed
`"{agent}:{connection}::fast"`.

`connect()` builds a fresh in-memory DuckDB per call, attaches each activated
artifact read-only with its key, registers a view per relation, and **then locks
the session down** before any agent SQL runs:

```sql
ATTACH '{artifact_path}' AS a_{n} (READ_ONLY, ENCRYPTION_KEY '{key}');
CREATE VIEW {name} AS SELECT * FROM a_{n}.{name};
SET enable_external_access = false;   -- after attach, before agent SQL
SET lock_configuration = true;
```

`execute_query(sql)` runs DuckDB SQL and returns a DataFrame.

The lockdown matters: with pandas denied filesystem access, **DuckDB SQL becomes
the attack surface**. Without it, generated SQL could `ATTACH` another
connection's artifact, `read_parquet('/etc/…')`, or exfiltrate via
`COPY (SELECT …) TO '/tmp/x.csv'`. Verify the exact pragma behavior on DuckDB
1.5 during the sandbox loop.

Registering **only activated relations** is the authorization boundary, and it
is structural: an agent cannot name a custom table it has not been given. This
is the same mechanism phase 2 will extend with RLS predicates.

`description` states the dialect (DuckDB SQL), the available relations, and each
relation's `as_of`.

## Agent context & codegen

- `schema_context_builder.py` renders custom tables under the fast connection.
- `tables_schema_section.py` adds `fast="true"` and `as_of="..."` to the table tag.
- `coder.py` needs **no prompt restructuring** — `<connection_clients>` is built
  from `client.description` (`coder.py:277`), so the fast client describes
  itself.

Hints worth carrying, in the description and schema context:
- **DuckDB SQL dialect** — source-dialect rules do not apply.
- **Scans are cheap.** Today's prompt pushes toward minimal, defensive queries
  because every one hits a remote source. Against a local relation the model
  should feel free to scan, use window functions, and make multiple passes.
  This is the main quality win for `create_data`.
- **`as_of`**, so the model can caveat freshness.
- Prefer the fast relation over a live equivalent when both are visible.

`create_data` and `inspect_data` need no code change — they consume `ds_clients`
and schema context, both of which carry the new information.

## Execution & observability

`_InstrumentedClient.execute_query` (`code_execution.py:560`) works unchanged —
the fast client is just another client — with two additions:

- `_captured_timings` (`:580`) gains `source: "fast"` and `as_of`, so freshness
  reaches the UI through a structure that already flows there.
- Skip `_enforce_rate_limit` (`:647`) on fast queries: that limiter protects the
  *source*, and a fast query never touched it. Still consume
  `_consume_data_bytes_quota` (`:708`) — the user did receive the data.

## API

```
GET    /connections/{id}/custom-queries
POST   /connections/{id}/custom-queries              {name, definition_sql, schedule}
POST   /connections/{id}/custom-queries/test         {definition_sql} -> preview + columns
PUT    /connections/{id}/custom-queries/{cq_id}
DELETE /connections/{id}/custom-queries/{cq_id}
POST   /connections/{id}/custom-queries/{cq_id}/refresh
```

All gated on `manage_connection` through the existing decorator
(`permissions_decorator.py:244` already maps resource `connection` →
`connection_id`). Routes live in `backend/app/routes/connection.py` alongside
the existing `/{connection_id}/tables` endpoint (`:890`).

`ConnectionDetailSchema` (`routes/connection.py:380`) gains
`custom_queries_count`.

## UI

**`ConnectionDetailModal.vue`** — a "Custom tables" section showing the count
and the list, with create / edit / delete.

**`/agents/:id/tables`** — custom tables appear alongside tables with a badge,
plus a "New custom table" action for admins holding `manage_connection` on that
connection. An agent may have several connections and the permission is
per-connection, so the action is enabled per row, not per page.

> The modal must state plainly that the effect is connection-wide:
> *"This custom table is created on Postgres (prod) and can be activated by any
> agent using that connection."* Creating it from an agent page makes it look
> agent-local; it is not.

**Create/edit modal** — opened by an **Add Custom** button in the tables list.
Wide modal, tabbed so phase 2 slots in without a redesign:

- **Query** — name, SQL editor, **Test**. The preview shows sample rows, the
  inferred columns, the row count, and the **estimated artifact size**, so the
  cost is visible before anyone schedules it hourly.
- **Cache** — schedule picker (`Every N minutes/hours` or `Daily at HH:MM`).
- **RLS** — phase 2.
- **Danger** — delete.

**Save requires a successful test run** — that is where `columns` comes from.
The Test action runs the admin's SQL wrapped in a `LIMIT`, never raw.

**Row detail:** `as_of`, row count, artifact size, last refresh status/error,
manual **Refresh**, **Delete**.

**Delete:** confirm, and warn when the query is activated on N agents.

## Security posture (v1, pre-RLS)

- `system_only` connections only; the option is hidden on `user_required`.
- Only activated relations are attached and registered in the DuckDB session.
- Audit-log create / update / delete / manual refresh.
- Artifacts are deleted when the custom table or its connection is deleted.

### Encryption is the sandbox boundary

Without it there is a real hole. Generated Python can read a plain artifact
directly — `pandas` is injected into the sandbox namespace
(`code_execution.py:888`), is not in `FORBIDDEN_MODULES` (`:262`), and while
`open` is blocked (`:276`) pandas' own IO does not use it. So
`pd.read_parquet('<path>')` would bypass the view catalog entirely.

Storing artifacts as **encrypted DuckDB files closes this in v1**: pandas cannot
open one, and the key never enters the sandbox namespace.

**Key management reuses existing machinery.** `settings.bow_config.encryption_key`
already backs Fernet for SMTP and external-platform secrets
(`services/email/secrets.py:17`). Generate a per-artifact key, store it
Fernet-encrypted on the `ConnectionTable` row, and decrypt it in the service
layer when attaching.

Defense in depth, still worth doing:
1. **Opaque UUID filenames**, so paths are not derivable.
2. Add `pd.read_*`, `np.load`, `np.fromfile` to the forbidden-call list in
   `CodeSecurityVisitor` (`code_execution.py:293`), so any attempt fails loudly
   rather than silently returning empty.
3. `enable_external_access=false` + `lock_configuration=true` on the serving
   session (see Query path) — with pandas locked out, DuckDB SQL is the
   remaining surface.

## Testing

**Unit**
- Model + migration; `(connection_id, name)` uniqueness.
- `refresh_schema` does not delete or overwrite `kind='bow'` rows.
- Refresh writes an encrypted DuckDB artifact and swaps atomically.
- An artifact cannot be opened without its key.
- `EXPLAIN`-based rejection fires above the configured row/byte budget.
- Extraction of a large result holds bounded memory (batch-sized, not result-sized).
- Failed refresh preserves the previous artifact and records the error.
- `FastQueryClient.connect` registers only activated relations; an unactivated
  custom table is not nameable.
- Schema context renders `fast` / `as_of`.
- Sandbox validator rejects `pd.read_parquet(...)`.
- Serving session refuses `ATTACH`, `read_parquet('/etc/...')`, and `COPY ... TO`.

**Integration**
- create → test → save → refresh → agent query returns rows from the artifact,
  asserting **no query was issued to Postgres** during the agent turn.
- Delete removes the artifact from disk.

**E2E** (via the `sandbox-feedback-loop` skill — used both while building and to
verify against a real Postgres)
- Create a custom table on the Postgres connection, ask the agent a question
  that uses it, assert the answer and the freshness indicator.
- **The assertion that matters most is the negative one:** once a custom table is
  active, an agent turn issues **zero queries to Postgres**. Verifiable from the
  backend logs, and it is the whole feature in a single check.

## As built — where the implementation diverged from this plan

The design above is what was intended; these are the things the build changed or
added, and the reasons are worth carrying forward.

- **Cached relations are attributed to the `::fast` client in schema context.**
  This is the most important delta and it was not in the plan. The context
  originally rendered a custom table under its *source* connection, and the
  coder is instructed to map a table's `<connection name>` onto a client_key
  suffix — so it sent generated SQL to the live client, where the relation does
  not exist. Every query against a cached relation failed. Cached relations now
  render under `<connection name="<conn>::fast" type="duckdb">`, and the
  renderer groups by `(connection_id, connection_name)` because the two client
  identities share one physical connection.
- **The planner is told to prefer cached relations.** With a live model in the
  loop, a cached relation was treated as a fallback: the agent scanned the raw
  tables first, hit the source, and got a *wrong* figure because its ad-hoc join
  left out the business definition the curated query encodes. Tables now render
  `cached="true"` / `as_of`, the admin's `description` reaches context (it was
  being dropped entirely), and `prompt_builder_v3` states the preference.
- **Activation is per agent and defaults off.** Creating a query activates it
  only on the agent it was created from; every other agent — and every new one —
  gets an inactive row. That activation is also the access boundary: a
  deactivated relation is absent from the DuckDB catalog, not filtered.
- **The authoring modal picks its connection.** An agent can have several, so
  the modal lists every accelerable one; switching clears the preview, since a
  preview taken on one dialect says nothing about another and a stale one would
  satisfy the save gate.
- **Schedules run in the org timezone**, and the row shows next run, last
  duration and cached row count.
- **Tool output badges cached reads** with a bolt in `create_data`,
  `inspect_data` and `describe_tables`.
- **Power BI custom tables are DAX, and the source was designed against a live
  tenant** (`fast/powerbi_source.py`). Three measured facts shaped it: the
  `executeQueries` endpoint truncates at 100,000 rows *or* 1,000,000 values with
  HTTP 200 and no flag (`GENERATESERIES(1, 150000)` came back as exactly 100,000
  rows; a 12-column table stopped at 83,333); DAX's `WINDOW` refuses to page a
  base table ("Relation parameter may have duplicate rows"); and `COUNTROWS` is
  one cheap request for an exact count. So the estimate is exact, the preview is
  a `TOPN`, and anything over the per-response ceiling is fetched in value
  windows over a numeric or date column picked from a sample, then verified
  against the count — a shortfall fails the refresh and keeps the previous
  artifact. Blank cursors are fetched separately rather than lost. A DAX text
  names tables but not the model, so the target semantic model is resolved from
  the tables it references against the indexed catalog, with an explicit pin
  (`metadata_json.target`, the modal's "Semantic model" picker) winning and an
  ambiguous match refused. Verified end to end: 250,000 synthetic rows in 7
  windows with every value present, and real model tables with dates typed as
  timestamps. The service principal bypasses Power BI's own RLS, which is why the
  `system_only` gate matters here; the platform's row policies apply on the copy.
- **Column types come from the source's declaration, then widen; they never
  narrow or round** (`sources.declared_decimals`, `loader._wider`). The
  artifact's columns are created from the first 50,000-row batch, so a type
  inferred from values broke later batches: `numeric(12,2)` stored as
  DECIMAL(7,2) failed the refresh on the first larger value, and a column
  that was NULL (or whole numbers, Oracle's bare NUMBER) in batch one became
  INTEGER and rounded later decimals. A DB-API description's precision/scale
  (matched by position, since SQLAlchemy folds Oracle/Snowflake names) now
  types the column, including on an empty or all-NULL refresh. A scale of 0
  is also how PyMySQL, pyodbc, Oracle and Snowflake declare integers, so it
  makes a column DECIMAL only when the column holds a `Decimal` (or the
  driver's type code is `Decimal`); Python ints stay BIGINT, since DuckDB's
  `.df()` turns DECIMAL into float64 and would round an id past 2**53. An
  undeclared
  or under-declared column is ALTERed to the narrowest exact type that holds
  both batches. Past 38 digits no DuckDB decimal holds both, so the column
  becomes DOUBLE, rows already written included, and the loader logs the
  WARNING `extraction.column_became_approximate` (`column`, `from_type`).
  Repeated column names (`a.id, b.id`) are stored as `id`, `id_1`, as DuckDB
  itself names a case-only repeat, and `columns` reports those stored names.

### Analysis Services and SAP BusinessObjects sources

Both are tested against fake endpoints only. **Neither has been run against a
live server**; each module's docstring separates what was read off the client
code from what is assumed.

| | Analysis Services (`fast/analysis_services_source.py`) | SAP BusinessObjects (`fast/businessobjects_source.py`) |
|---|---|---|
| Definition | a DAX query (`EVALUATE …`) on a Tabular model; MDX keeps the one-request path it had | JSON: `{"universe": "Sales", "objects": ["Country", "Sales revenue"]}` |
| Estimate | exact, `COUNTROWS` (one request) | exact, the result's `$count` (creates and runs the query, then deletes it) |
| What one response is | the whole result, no cap, parsed in memory | a page chosen by `$top` |
| Paging | value windows over a numeric/date column, each request wrapped in `TOPN(page + 1, …)` so the server bounds it; page = 100,000 rows or 1,000,000 values (ours) | `$skip`/`$top` over one created query, 10,000 rows a request (ours); a short page continues from what arrived |
| Truncation | total checked against the count; a response of `page + 1` rows is split, not stored | paging ends only at the count; an empty page before it fails the refresh |
| Cancel | stop sending windows (no XMLA session to cancel) | stop paging; the temporary query is deleted |
| Incremental | window written into the DAX (`FILTER`, or `{{since}}`/`{{until}}`); MDX filtered locally | query runs whole, window taken from the result |
| Request limit | `custom_table_max_requests` | `custom_table_max_requests` |

The Analysis Services source subclasses the Power BI one: the count / sample /
window / verify strategy belongs to DAX, and only the transport, the target
(a catalog instead of a semantic model) and what a response can carry differ.

### Saving a cited result as a custom table

A result the agent produced can be the starting point of a custom table. The
result card's action bar offers **Save as cached table** to the connection's
admins when the result is one query that the connection itself answered (not
several queries, not a failed one, not one already read from a custom table,
and not a parameterized one, whose card can be re-run with other values).
It opens the same dialog as the tables page, with that query, a name derived
from the result's title and the connection filled in; nothing is created until
the admin previews and saves.

- The card decides from the per-query timing record, which names the
  connection, agent and client that answered (`connection_id`,
  `data_source_id`, `client_key`).
- Whether the connection can host a custom table is asked of the connection:
  `GET /connections/{id}/custom-queries/capability` (`manage_connection`),
  which answers `supported: false` instead of refusing. The card does not go
  through the agent, because a connection admin need not manage the agent.
- The query text comes from
  `GET /connections/{id}/custom-queries/from-result/{tool_execution_id}`,
  which requires `manage_connection`, permission to view code, and the right
  to read that conversation: the rule on `GET /reports/{id}/completions`
  (its owner, an organization admin, or a collaborator on its project).
  Sharing an agent with the report's owner is not enough. It also refuses a
  result whose query has been edited since, because the card then shows a
  later run. The rule itself is `single_query_of` in
  `custom_query_service.py`, mirrored for visibility in
  `frontend/utils/cacheCandidate.ts`.
- A one-shot first load writes the rows read so far, and the source's count
  once known, to its run row. The dialog polls
  `GET /connections/{id}/custom-queries/progress?name=…`, which looks up that
  one table, so its cost does not grow with the number of custom tables on
  the connection. Progress is per batch, so a result that arrives in one
  response goes from zero to done.
- A create whose first load fails leaves the table with its error (as before);
  the dialog now says so and closes.

Run report: `docs/feedback-loops/save-as-cached-table.md`.

### Column metadata: what a cached table's columns mean

A custom table's columns carry what the connection's catalog already knows
about where they came from. It is built on publish and on every refresh from
rows indexing stored; no source is asked anything, so it works for any
connector whose catalog has descriptions or metadata (Postgres column
comments, Power BI measures, Priority titles).

- **Lineage** (`fast/lineage.py`, pure). Per output column: the indexed
  column it copies, or "derived" with its expression. SQL is read with
  sqlglot (plain and aliased columns, joins, CTEs and subqueries, `*`); DAX
  matches `Table[Column]` and `[Measure]` against the indexed model, resolved
  like the extraction target (pinned, else the one model that fits);
  Priority's OData and a BusinessObjects query map their fields one to one.
  A UNION, an unqualified column two joined tables share, a table name two
  schemas share, a relation whose columns are unknown: no lineage. A missing
  description is better than a wrong one.
- **What is stored**, on each entry of `ConnectionTable.columns`:
  `description` (the source's, or the expression of a derived column followed
  by what the columns it reads mean) and, in `metadata`, the source column's
  own metadata plus `source`, `additive`, `additive_reason`, `fingerprint`
  and, where they apply, `recompute`, `edited`, `inherited`, `drift`. The
  shape is documented at the top of `fast/column_metadata.py`.
- **Additivity** (`fast/additivity.py`). `true` for SUM, COUNT and plain
  arithmetic over them, and for a plain numeric column; `false` for
  COUNT(DISTINCT) / DISTINCTCOUNT, averages, ratios, MIN / MAX, time
  intelligence, share-of-total; `"unknown"` when the expression is unreadable
  or more than the classifier understands. A grouping key, an identifier
  (a primary or foreign key in the catalog) and a non-numeric column are
  `false` with a reason that produces no note. A measure that references
  other measures is followed through the model. The SQL walk follows the
  expression's structure, because where something happens decides the answer:
  `SUM(cents / 100.0)` and `SUM(cents) / 100.0` add up (a unit conversion),
  `SUM(a) / SUM(b)` does not (a ratio), and `SUM(cents) + 1` does not either
  (the constant is added once per group, so twelve monthly rows carry twelve).
  A reference to another query's computed output (a CTE, a subquery) is
  replaced by that computation before classifying, so `CAST(users AS BIGINT)`
  over a monthly `COUNT(DISTINCT ...)` is still a distinct count; when that
  computation cannot be read, the answer is `"unknown"`.
- **What the agent reads.** A note on the column,
  `aggregation="NOT ADDITIVE (distinct count): …"`, rendered by
  `tables_schema_section._aggregation_attr` in both render paths, only for a
  numeric column that must not be summed or a computed one nobody could
  classify. Where the right roll-up is known exactly the note gives it: a
  ratio whose whole expression is one division of two additive columns of the
  same table (`SUM(revenue) / SUM(orders)`; not `DIVIDE(a, b) * 100`, where
  that would be off by the factor), and a direct MIN or MAX (`MIN(lowest)`).
  It is a note on the data, not a prompt rule. The wording was settled
  against a live model: a first version that only said "do not SUM" was
  ignored when the user asked for a "total"
  (`docs/feedback-loops/cached-table-column-metadata.md`).
- **Description.** `column_metadata.describe_table` states the grain, the
  measures with their additivity, the sources and the schedule, by plain
  code. It is kept in `metadata_json["generated_description"]` and refreshed
  with the columns; agents read it only when the admin's own `description` is
  empty. The dialog shows it in the description field and saves an untouched
  one as empty, so it does not freeze.
- **Admin edits.** Description and additivity are editable per column in the
  dialog. The edits are the admin's settings, kept apart from what is derived
  (`metadata_json["column_overrides"]`), applied on every pass and marked
  `edited`, with what the source says kept under `inherited`. A create or
  update request carries the complete set in `columns`; a column left out
  follows its source again. Editing never reloads data
  (`CustomQueryService.redescribe`).
- **Drift.** The fingerprint covers the source's expression (its type when it
  has none; for a derived column, its expression and what it reads), not its
  description: a reworded comment follows silently. Same source, different
  fingerprint on a refresh: the column gets `drift`, the run gets a warning
  event, and the flag stays until an admin marks it reviewed.
- **Row-level security.** When a source table carries the indexer's
  `rowLevelSecurity` marker, the preview and the saved table report it
  (`rls_sources`) and the dialog points at the Row-level security tab.
  Source roles are **not** translated or imported (their definitions are
  usually unreadable), and **no data samples are stored** (they would sit
  outside the copy's row policy). Both were considered and rejected.
- **Existing tables** are described by their next refresh; nothing is
  migrated.

Run report: `docs/feedback-loops/cached-table-column-metadata.md`.

## Phases

1. **This doc** — custom tables, materialization, schedule, delete, count,
   Postgres, agent querying.
2. **RLS** — predicates bound to user attributes, per-session filtered catalog,
   "preview as user" tester, conformance check
   (live-under-user-credential vs. fast copy, diffed). Unlocks `user_required`.
3. **More connectors** — Oracle, SQL Server, then Salesforce and Snowflake.
   Extraction is already `execute_query`, so each is mostly dialect + watermark
   work. Salesforce is the biggest capability win: a materialized SOQL query
   becomes queryable with full SQL, escaping the no-JOIN restriction, the
   10k-row cap (`salesforce_client.py:44`), and API quotas.
4. **Incremental refresh** — a `{{last_watermark}}` token the admin places in
   their SQL, plus append mode. Avoids rewriting admin-authored SQL.

## Independent track — source protection

Not part of this feature, needs no design, and helps every connector whether or
not anything is accelerated:

- **Connection pooling.** Every query currently builds and disposes an engine.
- **Real query cancellation.** `_call_with_timeout` (`code_execution.py:615`)
  abandons the thread on timeout — the source keeps executing to completion.
  Use driver-level `cancel()`.
- **Result streaming / LIMIT pushdown.** `execute_query` materializes the full
  result before `format_df_for_widget` truncates to the org row cap.
- **Per-connection concurrency ceiling**, beside the existing `rate_limit_*`
  fields on `Connection`.

This track can ship before, during, or after phase 1.
