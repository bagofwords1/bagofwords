# Custom queries + FAST acceleration (Postgres) — design

A **custom query** is a named, materialized relation on a connection. An admin
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
- Create / test / save / delete custom queries on a connection (`manage_connection`).
- Materialize each to Parquet on a schedule (interval or daily-at-time).
- Agents query them through a DuckDB-backed client, in DuckDB SQL.
- Schema context + codegen hints so the model knows a relation is local and how fresh it is.
- Custom-query count surfaced on connection detail.
- `system_only` connections only.

**Out (v1):**
- **RLS — phase 2.** No row filtering; a custom query returns the same rows to every user of an agent that has it activated.
- Incremental / append refresh — **full reload only**.
- FAST on introspected tables (raw `ConnectionTable` rows) — v1 accelerates *queries*, not tables.
- Connectors other than Postgres.
- `user_required` connections.
- Live (non-materialized) views. A custom query is always materialized; inlining its SQL into agent-generated SQL would require rewriting generated SQL, which we are deliberately not doing.

## Data model

Custom queries live in `ConnectionTable` rather than a new model, so activation
(`DataSourceTable.is_active`), per-user overlays, `table_stats`, and schema
context rendering all keep working with no changes.

`backend/app/models/connection_table.py` gains:

```
kind                      String   'table' | 'view'   default 'table', not null
definition_sql            Text     nullable    -- views only, source dialect
refresh_schedule_mode     String   'interval' | 'time'
refresh_interval_minutes  Integer  nullable
refresh_at_time           String   nullable    -- "HH:MM" (24h)
last_refreshed_at         DateTime nullable
last_refresh_status       String   nullable    -- ok | error | running
last_refresh_error        Text     nullable
artifact_path             String   nullable    -- opaque parquet filename
artifact_bytes            BigInteger nullable
```

Schedule field names deliberately mirror the existing block on `Connection`
(`reindex_schedule_mode`, `reindex_interval_minutes`, `reindex_at_time` —
`models/connection.py:48-50`) so the UI and scheduler patterns carry over.

`no_rows` (already present) holds the materialized row count. `columns` is
populated from the test run and is required — the agent's schema context, the
DuckDB schema, and phase-2 RLS predicates all depend on it. `pks` / `fks` are
stored as empty lists.

New unique constraint: `(connection_id, name)` — a custom query must not collide
with an introspected table name on the same connection.

Migration under `backend/alembic/versions/`.

### Trap to guard

`ConnectionService.refresh_schema` (`connection_service.py:1061`) upserts
`ConnectionTable` rows from live introspection. It **must exclude
`kind='view'` rows** from its upsert and any stale-row sweep, or a scheduled
reindex will silently delete every custom query. This needs an explicit test.

## Storage & refresh

- Artifacts at `uploads/fast/{connection_id}/{uuid4}.parquet`. Filenames are
  **opaque UUIDs, not content hashes** — see Security below.
- Write to `.tmp`, then atomic rename (the QVD path already does this;
  `qvd_client.py`).
- Refresh is `client.execute_query(definition_sql)` → DataFrame → pyarrow →
  Parquet. **No new client method** — every SQL client already implements
  `execute_query`, which is the main reason for query-shaped acceleration.
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

## Query path

A new `FastQueryClient` (DuckDB-backed) is constructed in
`DataSourceService.construct_clients` (`data_source_service.py:2143`) for each
connection that has at least one **activated** custom query, keyed
`"{agent}:{connection}::fast"`.

`connect()` builds a fresh in-memory DuckDB per call and registers only the
activated relations — the same shape as `qvd_client.py:606`:

```sql
CREATE VIEW {name} AS SELECT * FROM read_parquet('{artifact_path}');
```

`execute_query(sql)` runs DuckDB SQL and returns a DataFrame.

Registering **only activated relations** is the authorization boundary, and it
is structural: an agent cannot name a custom query it has not been given. This
is the same mechanism phase 2 will extend with RLS predicates.

`description` states the dialect (DuckDB SQL), the available relations, and each
relation's `as_of`.

## Agent context & codegen

- `schema_context_builder.py` renders custom queries under the fast connection.
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

**`ConnectionDetailModal.vue`** — a "Custom queries" section showing the count
and the list, with create / edit / delete.

**`/agents/:id/tables`** — custom queries appear alongside tables with a badge,
plus a "New custom query" action for admins holding `manage_connection` on that
connection. An agent may have several connections and the permission is
per-connection, so the action is enabled per row, not per page.

> The modal must state plainly that the effect is connection-wide:
> *"This custom query is created on Postgres (prod) and can be activated by any
> agent using that connection."* Creating it from an agent page makes it look
> agent-local; it is not.

**Create/edit modal:** name, SQL editor, **Test** (runs it, shows a preview and
the inferred columns), schedule picker (`Every N minutes/hours` or `Daily at
HH:MM`), Save. **Save requires a successful test run** — that is where `columns`
comes from.

**Row detail:** `as_of`, row count, artifact size, last refresh status/error,
manual **Refresh**, **Delete**.

**Delete:** confirm, and warn when the query is activated on N agents.

## Security posture (v1, pre-RLS)

- `system_only` connections only; the option is hidden on `user_required`.
- Only activated relations are registered in the DuckDB session.
- Audit-log create / update / delete / manual refresh.
- Artifacts are deleted when the custom query or its connection is deleted.

**Known gap — mitigated, not closed.** Generated Python can read an artifact
directly:

```python
pd.read_parquet('/app/uploads/fast/<connection>/<uuid>.parquet')
```

`pandas` is injected into the sandbox namespace (`code_execution.py:888`) and is
not in `FORBIDDEN_MODULES` (`:262`); `open` is blocked (`:276`) but pandas' own
IO does not use it. So an agent could in principle read an artifact belonging to
a custom query it has not been given.

v1 mitigations:
1. **Opaque UUID filenames** (not deterministic hashes), so paths are not derivable.
2. Add `pd.read_*`, `np.load`, `np.fromfile` to the forbidden-call list in
   `CodeSecurityVisitor` (`code_execution.py:293`), so an attempt fails loudly.

Full closure requires **encrypted artifacts**, and lands with RLS in phase 2.
Until then, treat a fast artifact as readable by any agent on the box, and do
not use custom queries for data that some agents on the same connection must not
see. This is acceptable in v1 precisely because there is no RLS yet — every
consumer of a `system_only` connection already shares one identity.

## Testing

**Unit**
- Model + migration; `(connection_id, name)` uniqueness.
- `refresh_schema` does not delete or overwrite `kind='view'` rows.
- Refresh writes Parquet and swaps atomically.
- Failed refresh preserves the previous artifact and records the error.
- `FastQueryClient.connect` registers only activated relations; an unactivated
  custom query is not nameable.
- Schema context renders `fast` / `as_of`.
- Sandbox validator rejects `pd.read_parquet(...)`.

**Integration**
- create → test → save → refresh → agent query returns rows from the artifact,
  asserting **no query was issued to Postgres** during the agent turn.
- Delete removes the artifact from disk.

**E2E** (via the `sandbox-feedback-loop` skill)
- Create a custom query on the Postgres connection, ask the agent a question
  that uses it, assert the answer and the freshness indicator.

## Phases

1. **This doc** — custom queries, materialization, schedule, delete, count,
   Postgres, agent querying.
2. **RLS** — predicates bound to user attributes, per-session filtered catalog,
   **encrypted artifacts**, "preview as user" tester, conformance check
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
