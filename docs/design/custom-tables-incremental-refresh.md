# Custom tables: run history, incremental refresh, normalization — plan

Status: **phases 1–3 implemented** (see "As built" below; results in
`docs/feedback-loops/custom-table-incremental-refresh.md`). Phase 4 is not
built. Scope agreed: build phases 1–3 now; phase 4
(transform layer over DuckDB) is future work, but every phase below must leave
the door open for it (see "Phase-4 readiness").

Verification follows `.agents/skills/sandbox-feedback-loop/SKILL.md`: each
phase ships a deterministic Loop A (no external services) and a live Loop B
against real Postgres and SQL Server engines in Docker. Results are recorded in
`docs/feedback-loops/custom-table-incremental-refresh.md`.

---

## As built — where the implementation differs from the plan below

The plan is kept as written; these are the places the work taught us
something and the code went another way.

| Plan | Built | Why |
|---|---|---|
| Window values passed as bound parameters | Rendered as **typed literals** per dialect (`incremental.literal`), from values we produce (datetime / date / number); text cursors are charset-checked | One text serves the read-only guard, the cost estimate, the preview, the extraction and the run log — the log shows exactly what ran. Estimators (`SHOWPLAN_XML`, `EXPLAIN`) do not all accept parameters. |
| Modes `append`, `merge`, `replace_window` | Two: **merge** (by key) and **append**, where append *replaces the tail* (deletes rows at/after the window start, inserts what was read) | Tail replacement makes append idempotent without a key, so `replace_window` is the same operation with a `grain`. |
| Refuse incremental when the cursor has NULLs | NULL-cursor rows are **carried**: a wrapped query fetches them with the open-ended window on every run; the count is logged | Refusing would block ordinary messy tables. A query that places its own filter excludes them by its own predicate, which the UI says. |
| A schema change on an increment fails the run | The run **rebuilds in full** and logs why; a type that merely widens is altered in place | A table that errors every hour until someone notices is worse than one slow refresh. |
| Chunks cut evenly between min and max | Chunks planned **by row density** (`plan_by_density`): the busiest window is halved and counted until none exceeds the target | Two outlier rows (1970, 2099) stretched the range until one chunk held the whole table. |
| Duplicate keys reject merge at setup | Rows sharing a key **collapse to the newest**, with a warning on the run | Checking uniqueness up front means a full scan of the source; the collapse is what an upsert means, and it is reported. |
| `CustomTableRun.connection_table_id` as a foreign key | A soft reference | Custom tables are soft-deleted and history is purged by age. |
| Run statuses include `skipped`, `interrupted` stored | `partial` (a load paused part-way) is stored; `interrupted` is derived for a run that stopped reporting | Nothing produces a skipped run: a queued refresh waits. |
| Loop B measures 1 / 2 / 5 / 10 GiB incl. object storage | Local copy-forward measured to 1.77 GiB (~7 s/GiB) | No object store in the sandbox; the larger points are still owed before the cap is raised. |
| Partial loads are pod-local; another pod restarts the load | Another pod **hands the turn back** to the pod holding the part file for 45 minutes after its last progress, then restarts it | Restarting on every hand-off could keep a long load from ever finishing. |
| A failing full rebuild is retried on each tick | After three failures without progress it is set aside for a day and increments continue | A served table must not freeze because its rebuild cannot finish. |
| Incremental offered only where a window can be written; Salesforce out of scope | **Every queryable connector is cacheable and incremental.** A connector that can filter at the source says how (SQL dialect, its own writer for SOQL / DAX, or JSON request fields); one that cannot has its query run whole and the window taken from the result | The owner's requirement: it should behave like an ETL product, not a list of supported sources. Salesforce got a paged SOQL source so it is not bound by the agent's 10,000-row cap. |
| PR slicing 1a–5 | One branch, built in that order | — |

Also added beyond the plan: the row-drop guard is on by default (50 %) for new
tables only; a paused load schedules its own continuation; request-type
connectors align windows to their rollup bucket; and three defects in the
existing full-refresh path were fixed (types fixed by the first batch,
duplicate column names dropped, SQL Server `(MAX)` width estimate).

---

## 1. Why (product)

An admin who caches `SELECT … FROM orders` today pays a **full re-extraction
on every refresh**: one long query against the source (up to 30 min,
`extractor.DEFAULT_MAX_SECONDS`), holding the per-connection volume lock so
every sibling custom table on that connection waits behind it. Large tables
either hit the 5M-row / 2 GiB caps or make the source DB "feel stuck". There is
also no history: the row only remembers the *last* refresh status, so nobody
can answer "what did yesterday's 03:00 run load?".

Target: first load in resumable chunks; subsequent refreshes fetch only rows
at/after a stored checkpoint (minus a lookback); every run leaves an auditable
record of what it fetched and produced.

## 2. Current state (validated, cite before changing)

| Fact | Where |
|---|---|
| Refresh = estimate → full extract into a **new uuid file** → conditional-UPDATE swap → delete old | `backend/app/services/custom_query_service.py:659` (`refresh`), swap at `:752` |
| Extraction streams 50k-row Arrow batches into DuckDB; caps rows/bytes/seconds | `backend/app/data_sources/fast/extractor.py` (`extract_to_artifact`, `BATCH_ROWS`) |
| Source protocol: `estimate / preview / stream_batches` | `backend/app/data_sources/fast/sources.py` (`ExtractionSource`, `SqlAlchemySource`) |
| Artifacts are **write-once**; readers cache by path (`fresh=False`) and object storage persists them | `fast_client.py` (`connect`, `ensure_local(..., fresh=False)`), `artifacts.py`, `services/storage/__init__.py` |
| One refresh per **connection** at a time (kernel file lock) | `artifacts.py` (`refresh_lock`) |
| Windowed extraction over a date column already exists (PostHog, Power BI): `SELECT * FROM (<q>) WHERE col >= lo AND col < hi` | `posthog_source.py` (`_fetch`, `_windowed`), `powerbi_source.py` (`_windowed`) |
| Run-history pattern with own-session writes, retention, interrupted detection | `services/dashboard_refresh_history.py`, `models/dashboard_refresh_run.py` |
| No checkpoint / mode / key / lookback fields | `models/connection_table.py` |
| Non-SQL connectors take JSON specs with native time windows (vROps `start_time`/`end_time` epoch ms) | `data_sources/clients/aria_operations_client.py:599` |
| Pinned DuckDB has `MERGE INTO` and `COPY FROM DATABASE` (probe: 2M-row encrypted copy 0.23s, 11k-row merge 0.01s, local) | `pyproject.toml` `duckdb>=1.5.4,<1.6` |

**Invariant to preserve:** a refresh never mutates a served file. Incremental
runs must *copy the previous artifact → apply delta → publish a new uuid →
swap*, so failure/cancel keeps the old data serving and multi-pod caching
stays correct.

---

## 3. Phases

### Phase 1 — Run history + pre-publish checks

**Model** `CustomTableRun` (new table, migration `cqruns01`):

| Column | Notes |
|---|---|
| `id`, `organization_id`, `connection_table_id` (FK, index) | |
| `trigger` | `create` · `schedule` · `manual` · `sql_change` · `resume` |
| `mode` | `full` · `incremental` · `backfill` |
| `status` | `running` · `success` · `failed` · `cancelled` · `skipped` · `interrupted` |
| `started_at`, `finished_at`, `elapsed_ms` | |
| `triggered_by_user_id` | null for schedule |
| `window_from`, `window_to` (JSON typed values) | checkpoint before / after — "what period did this run cover" |
| `rows_read`, `rows_inserted`, `rows_updated`, `rows_deleted`, `artifact_bytes` | |
| `artifact_path` | the file this run published (null if not published) |
| `rendered_query` | text actually sent, window values filled (secrets never present — query text only) |
| `schema_fingerprint` | hash of output columns+types (phase-4 drift detection) |
| `inputs` (JSON) | upstream references; today `{"connection_id": …}`; phase 4: upstream table ids + their run ids |
| `chunks` (JSON) | per-chunk `{from,to,rows,status}` — doubles as resume state |
| `events` (JSON, capped ~200 entries / 64 KB) | structured log: estimate, chunk, merge, checks, swap, error |
| `error` (clipped) | |

**Service** `services/custom_table_runs.py` mirroring `dashboard_refresh_history`:
own short session per write (a rolled-back refresh keeps its row), never
raises into the refresh, `RETENTION_DAYS` purge job, rows left `running` past
`max_seconds + margin` marked `interrupted` (server died mid-run).

**Pre-publish checks** (run on the new file *before* the swap; fail ⇒ keep old,
run = `failed`, reason in `events`):
- row count dropped more than N% vs previous (configurable; off for tables
  that legitimately shrink),
- columns removed / type changed vs previous `schema_fingerprint` (warn on
  incremental, fail on incremental merge because the copy-forward would be
  inconsistent → forces a full refresh),
- schema of the delta vs the stored table (see "Schema pinning" in phase 2).

(NULL cursor values cannot be a pre-publish check: the window predicate
already filters those rows out, so they never reach the file. See "NULL
cursors" in phase 2.)

**Rows carry lineage**: every materialized row gets hidden `_bow_run_id`
and `_bow_loaded_at` columns; `FastQueryClient` registration exposes a
view without them (agents never see them; `columns` stored on the row excludes
them). Verified feasible: all three branches of
`FastQueryClient._register_relation` (`fast_client.py:176`) are `SELECT *`
and become `SELECT * EXCLUDE (_bow_run_id, _bow_loaded_at)`; RLS validation
must reject these names as policy columns. Enables "rows added by run X" and phase-4 incremental transforms.

**API/UI**: `GET /connections/{id}/custom-queries/{cq}/runs` (paged) and
`/runs/{run_id}`; a **Runs** tab in `CustomQueryModal.vue` / tables page —
list (time, trigger, mode, status, rows ±, duration) → detail (window, query,
events). Buttons: Refresh now, Full refresh, Resume (failed initial load).
i18n keys in `locales/{en,es,he}.json` (all three, identical shape).

### Phase 2 — Incremental refresh (connector-agnostic core)

**Per-table config** (columns on `ConnectionTable`, migration `cqincr01`):
`refresh_mode` (`full`|`append`|`merge`|`replace_window`),
`cursor_column`, `cursor_kind` (`timestamp`|`date`|`number`), `unique_key`
(list), `lookback` (value + unit), `initial_value`, `chunk_size`
(e.g. `1 month` / `100000` ids), `full_refresh_every_days`,
`checkpoint` (JSON typed `{kind,value,tz}`), `definition_hash`.

**Core (shared, never per connector):**
1. Decide mode: full if no checkpoint, `definition_hash` changed, schema drift,
   `full_refresh_every_days` elapsed, or user asked; else incremental.
2. Window = `[checkpoint − lookback, ∞)`; initial/backfill = list of chunks
   `[initial_value, …]` by `chunk_size`.
3. For each window/chunk: ask the source for rows (step "Window interface"),
   stream into a **staging** table in the new artifact; record chunk in run.
4. Apply: `append` → INSERT; `merge` → `MERGE INTO t USING staging ON key`;
   `replace_window` → `DELETE WHERE cursor in window` + INSERT (for GROUP BY
   rollups / no key).
5. New checkpoint = `SELECT max(cursor) FROM t` **on our copy** (type from
   Arrow, no clock skew). Persist checkpoint in the **same conditional UPDATE
   as the swap** — data and checkpoint move together or not at all.
6. Caps: row/byte caps apply to the artifact total; the time cap applies per
   run. An initial load that exceeds the time cap ends `failed` with completed
   chunks recorded; **Resume** continues from the last completed chunk
   (partial artifact kept as an unpublished `.tmp`-equivalent referenced by the
   run, GC-protected for 24h).
7. Lock: release the per-connection volume lock **between chunks** of an
   initial load so sibling tables are not starved (re-acquire per chunk).

**Window interface** (one new method on `ExtractionSource`, default provided):

```python
def stream_window(self, query, window: Window, batch_rows) -> Iterator[pa.Table]
# Window = {column, kind, lo, lo_inclusive, hi, hi_inclusive}
```

How each family applies it — chosen by declaration on the client, not sniffed:

| Family | Mechanism | Connectors |
|---|---|---|
| Placeholder in query text: `{{since}}` / `{{until}}` | Bound **parameters** via SQLAlchemy `text().bindparams` (driver handles literal + type) | Postgres, MySQL/MariaDB, SQL Server, Oracle, Snowflake, Trino, Fabric, SQLite, Sybase |
| Placeholder, native params | BigQuery `ScalarQueryParameter`, ClickHouse params | BigQuery, ClickHouse |
| Placeholder, rendered literal (no bind support) | per-source `render_literal(value, kind)` | Salesforce SOQL (`2026-11-01T00:00:00Z`), Power BI DAX, PostHog HogQL |
| JSON spec window fields | client declares `INCREMENTAL_WINDOW = {"start": "start_time", "end": "end_time", "format": "epoch_ms"}`; core writes values into the spec | vROps (Aria Operations), CloudWatch, cost APIs, alert feeds |
| Auto-wrap (convenience, SQL only, no placeholder) | `SELECT * FROM (<q>) bow_sub WHERE col >= :since` — UI warns pushdown is not guaranteed | SQL dialects that accept derived tables |
| None | fetch all, filter locally in DuckDB — **correct but saves no source load**; UI says so | anything else |

Placeholder semantics:
- Two placeholders, `{{since}}` and `{{until}}`, **always bound**. A full run
  binds sentinels (kind minimum / maximum, or `initial_value`) — no
  dialect-specific "always true" rewrite is needed. Chunked loads require
  `{{until}}`; with only `{{since}}` the table can be incremental but its
  first load is a single query.
- Placeholders are rendered **before** every path that touches the query:
  `extractor.estimate`, `extractor.preview`, and `sql_guard.ensure_read_only`
  (`extractor._source`). The guard must see rendered text — raw `{{…}}` would
  fail the sqlglot parse and silently downgrade to the keyword check
  (`sql_guard.check_sql`).

**Rules the core enforces (each found in review, each needs a Loop A test):**
- **NULL cursors.** Rows whose cursor is NULL never match the window and
  would be silently absent. At setup and on each full refresh, probe the null
  count where the source can answer (`SELECT count(*) … WHERE col IS NULL`
  via wrap; PostHog already does this in `_range`) and refuse incremental
  mode if non-zero; where it cannot be probed, the UI states the rule.
- **Checkpoint cap.** New checkpoint = `min(max(cursor), run start)` for
  timestamps. One bad future-dated row (`updated_at = 2099`) would otherwise
  move the checkpoint past all real data and no refresh would ever load again.
- **Dedup the delta before merging.** Verified on the pinned DuckDB: `MERGE`
  with two delta rows for one key raises no error and keeps an arbitrary one.
  Staging is reduced to the latest row per key (by cursor) first; a
  `unique_key` that is not unique in the source is rejected at setup.
- **Schema pinning.** `SqlAlchemySource` infers Arrow types per batch from
  Python values (`sources.arrow_table`), so a delta can arrive with different
  types than the stored table (all-NULL column → `null`, decimal → `double`).
  The delta is cast to the stored schema **by column name**, never by
  position; a cast failure or added/removed column fails the run and requires
  a full refresh. (Verified: compatible drift inserts cleanly, incompatible
  raises a conversion error.)
- **`replace_window` aligns to a grain.** The window start is truncated to
  the table's grain (day/month). Recomputing a daily rollup from a window
  that starts mid-day would publish a partial day as if it were complete.
- **One run per table.** Releasing the connection lock between chunks lets a
  scheduled tick for the *same* table start; a per-table claim prevents it.
  A scheduled tick that finds a partial initial load **resumes** it rather
  than starting over, so a large table converges without manual clicks.
- **Counts.** `rows_inserted` / `rows_updated` come from
  `MERGE … RETURNING merge_action` (verified available).
- **Backward compatible.** `refresh_mode` defaults to `full`; existing tables
  behave exactly as today until an admin opts in.

**Multi-pod / object storage:**
- Copy-forward needs the previous artifact locally: `storage.ensure_local`
  before the copy (a scheduled run can land on a pod that never served it).
- Each refresh still uploads the **whole** new file (`storage.persist`), so
  incremental saves source load and time, not upload bytes. Acceptable at the
  current 2 GiB cap; revisit (segment files) only if the cap is raised.
- A partial initial-load file is pod-local. Resume on the same volume
  continues; resume on another pod restarts from chunk 1 unless the partial
  is persisted per chunk (open question 6).

Generic `ExecuteQueryWindowSource`: for API connectors with no native source,
runs `client.execute_query(spec_with_window)` per window and halves the window
when a response hits the connector's row ceiling (PostHog algorithm,
generalized). This is what makes non-SQL connectors eligible at all; adding
one becomes "declare `INCREMENTAL_WINDOW` + a row ceiling" instead of a new
source class. Accelerable-type gating (`ACCELERABLE_TYPES`) extends to
"declares a window".

**Time zones**: tz-aware → UTC; naive (common on SQL Server `datetime2`) →
stored naive, compared naive, never mixed. Lookback absorbs DST/skew/late
commits. SQL Server: recommend `rowversion` as a numeric cursor in the UI hint.

**Out of scope for phase 2, tracked separately**: Salesforce acceleration
(no extraction source today; `execute_query` caps at 10k rows; Bulk API 2.0
for initial loads; `getDeleted()` for deletes).

### Phase 3 — Normalization stage

One stage between source batches and DuckDB, replacing per-source ad hoc code:
UTC timestamps, decimal widening (`sources.arrow_table`), enum-as-string
(ClickHouse), nested-record flattening (Salesforce `flatten_record`), column
name sanitation + collision handling, stable type mapping table. Output
contract documented: *every raw artifact looks the same whatever the
connector* — this is the contract phase 4 SQL is written against.

### Phase-4 readiness (what we must do now so a transform layer fits later)

Phase 4 = derived tables defined in DuckDB SQL over other custom tables
(including across connections), rebuilt when inputs refresh. To keep that
open without building it:

1. **Pipeline as steps**: refactor `refresh()` into
   `extract → normalize → validate → publish` with `extract` pluggable. A
   transform node later supplies an `extract` that runs DuckDB SQL over
   attached upstream artifacts; normalize/validate/publish/run-history are
   reused unchanged.
2. **Don't deepen the connection coupling**: new code takes a *node*
   (`ConnectionTable`) and reaches the connection only inside `extract`.
   Lock keyed by **source** (connection) *and* **node** — so a future
   connection-less node has a lock domain. Artifact dir stays per connection
   for now; note the future org-level dir.
3. **Run records carry `inputs` and `schema_fingerprint`** → lineage and drift
   detection between nodes come for free.
4. **Publish emits an event** (`custom_table.published {table_id, run_id}`)
   via a tiny in-process dispatcher with no subscribers today; phase 4's
   dependency scheduler subscribes instead of polling.
5. **`_bow_run_id` / `_bow_loaded_at` on rows** → incremental transforms can
   process only rows from new upstream runs.
6. **Security**: keep RLS compile at serve time per relation; a derived node
   will need the intersection of its inputs' policies and an org-level owner —
   do not add any shortcut that assumes one connection per node in RLS code.
7. `kind` stays a string; reserve `bow_derived` (no code).

---

## 4. Feedback loops

### Loop A — deterministic (no Docker, no network)

Extends the existing harness `tools/agent/custom_table_artifact_loop.py` /
`tests/unit/test_fast_artifact_lifecycle.py` (real SQLite source, real
encrypted DuckDB, real sessions):

```sh
cd backend && uv sync --frozen --extra dev
uv run python ../tools/agent/custom_table_artifact_loop.py            # existing suite stays green
uv run pytest tests/unit/test_custom_table_runs.py tests/unit/test_incremental_refresh.py -q
```

Invariants to assert (general, per `backend/tests/AGENTS.md` — not one magic case):
- every refresh path (ok / source error / cancel / delete-race / failed swap)
  leaves exactly one terminal run row with consistent counts;
- incremental run reads only rows with cursor ≥ checkpoint − lookback (assert
  via the source's executed SQL + bound params captured by a recording client);
- after N incremental runs the artifact equals a full refresh of the same
  source state (property: `full(source) == fold(incremental runs)` for append
  and merge; random inserts/updates generated per test);
- duplicate cursor values at the boundary are neither lost nor duplicated;
- failed checks / failed chunk ⇒ old artifact still served, checkpoint unchanged;
- resume after a mid-chunk failure produces the same artifact as an
  uninterrupted load;
- SQL change ⇒ next run is `full`;
- served relation never exposes `_bow_*` columns;
- JSON-window path: a fake API client with a row ceiling — window halving
  covers the range exactly once.

### Loop B — live engines (Docker)

Docker CLI is present in cloud sandboxes but the daemon may not be running
(observed: no `/var/run/docker.sock`). Start it (`dockerd &`) or use an
existing server via env URLs. Fallback without Docker: `apt-get install
postgresql` for the Postgres leg; SQL Server needs a container.

The SQL Server leg also needs Microsoft's ODBC driver on the host
(`mssql_client.py:92` builds `ODBC Driver <n> for SQL Server`); it is **not
installed** in the cloud sandbox (no `odbcinst`). Install `msodbcsql18` from
packages.microsoft.com as the first step — if the sandbox network policy
blocks that host, the SQL Server leg cannot run there and must run in CI or
locally. Postgres does not have this dependency, so it is the primary leg.

```sh
docker run -d --name bow-pg -e POSTGRES_PASSWORD=pw -p 55432:5432 postgres:16
docker run -d --name bow-mssql -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD='Str0ng!Passw0rd' \
  -p 51433:1433 mcr.microsoft.com/mssql/server:2022-latest
export INCR_LOOP_PG_URL=postgresql://postgres:pw@localhost:55432/postgres
export INCR_LOOP_MSSQL_URL='mssql+pyodbc://sa:...@localhost:51433/master?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes'
cd backend && uv run python ../tools/agent/custom_table_incremental_loop.py
```

(Passwords above are throwaway local-container values; real credentials only
via env, never committed.)

New harness `tools/agent/custom_table_incremental_loop.py` seeds per engine:
`orders(id pk, customer_id, amount, status, updated_at indexed)` ~2M rows over
24 months; SQL Server variant adds `rv rowversion` and uses naive `datetime2`.
Scenario, asserted step by step against the run records **and** the source:

1. Create custom table (merge, key `id`, cursor `updated_at`, chunk 1 month,
   lookback 1 day) → initial load completes as 24 chunks; artifact count == source count.
2. Kill the backend mid-initial-load → run `interrupted`; Resume → completes;
   artifact == source.
3. Insert 1k rows, update 500 old rows (bump `updated_at`) → refresh →
   `rows_read ≈ delta + lookback slice`, not 2M; artifact == source.
4. **Pushdown proof**: Postgres `pg_stat_statements` / `EXPLAIN` of the
   rendered query shows an Index Scan on `updated_at`; SQL Server
   `SHOWPLAN_XML` shows an Index Seek. A wrapped (auto-wrap) query is measured
   too, so the docs can state where pushdown fails.
5. Late-arriving row (old `updated_at` inside lookback) → picked up;
   outside lookback → not picked up until the periodic full refresh (documented).
6. Hard delete → still present after incremental; gone after full refresh.
7. Source lock impact: during an initial load, a concurrent sibling table
   refresh on the same connection completes between chunks (lock released).
8. SQL Server `rowversion` numeric cursor variant of steps 3–4.
9. GROUP BY daily rollup with `replace_window` → last N days recomputed,
   older days untouched; equals a full recompute.

Record wall-clock + rows read per refresh, full vs incremental, as the
evidence KPI.

### Loop C — UI (stack + Playwright)

`tools/agent/boot_stack.sh`, `seed_org.py`, Postgres container as the
connection. Drive: enable incremental in `CustomQueryModal.vue`, run, open
Runs tab, inspect a run, Full refresh, Resume. Before/after screenshots per
the **ui-evidence** skill (en + he for RTL).

### Optional Loop D — non-SQL

Reuse the existing Aria sandbox (`tools/agent/aria_sandbox_setup.py`) or a
small mock serving a metrics endpoint with a row ceiling: create a vROps
custom table, assert windowed initial load, then incremental fetch of only the
latest intervals (lookback covers the still-filling interval).

---

## 5. Delivery order & PR slicing

| PR | Content | Loops |
|---|---|---|
| 1a | Pipeline-steps refactor of `refresh()` — **no behavior change**, existing lifecycle suite must pass untouched | A |
| 1b | `CustomTableRun` + run API + Runs tab + pre-publish checks + `_bow_*` columns + publish event | A (new run invariants), C |
| 2 | Window interface, placeholder binding for SQLAlchemy dialects, checkpoint/lookback, append/merge, copy-forward publish | A, B (Postgres + SQL Server) |
| 3 | Chunked resumable initial load, lock release between chunks, `replace_window` | A, B steps 2, 7, 9 |
| 4 | Generic `ExecuteQueryWindowSource` + vROps declaration; BigQuery/ClickHouse/PostHog/Power BI window methods | A, D |
| 5 | Normalization stage consolidation | A + rerun B |

Each PR: feedback-loop section updated, release-notes skill for user-facing ones.

## 6. Open questions for the owner

1. Default lookback per kind (proposal: 1 day for timestamps, 0 for numeric).
2. Default periodic full refresh (proposal: weekly, at the table's scheduled time).
3. Run retention (proposal: 30 days like dashboard refresh history) and
   whether to keep N previous artifacts for one-click restore (storage cost).
4. Should auto-wrap exist at all, or placeholder-only for clarity?
5. Row-drop threshold for the pre-publish check (proposal: fail on >50% drop, per-table override).

## 7. Decisions (owner, 2026-11-11)

- **Partial initial loads are pod-local.** Resume continues on the same
  volume; on another pod the load restarts from the first chunk. No per-chunk
  upload.
- **Size caps stay** at 5M rows / 2 GiB (`extractor.DEFAULT_MAX_ROWS/BYTES`)
  for the first release. Loop B additionally measures copy-forward, upload and
  download time at 1 / 2 / 5 / 10 GiB; a per-organization cap (with a longer
  minimum refresh interval for large tables) is decided from those numbers.
  Beyond that, the path is multiple files per table (base + delta segments,
  compacted periodically) behind the `publish` step — not built now.
- Questions 1–5 above use the proposed defaults until the owner says otherwise.
