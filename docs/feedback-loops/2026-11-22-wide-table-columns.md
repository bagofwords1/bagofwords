# Feedback Loop — "a table with thousands of columns floods the agent's context" (BOW-215, steps 1–3)

An agent on a search index with ~5,000 fields put **every field into every
planner call** once the index ranked into the top-k tables. This loop builds
three very wide synthetic sources, measures the context before the change,
implements steps 1–3 of BOW-215 (column usage tracking, a per-table column cap
ranked by usage, column search in `describe_tables`), and verifies the result
with real measurements, a real LLM (Claude Haiku 5.5, `claude-haiku-5-5`), unit
and e2e tests, and Playwright runs through the UI as a logged-in user.

Steps 4–6 of the ticket (Elastic schema shrinking, admin pattern-exclude, an
agent schema-edit tool) are **not** built here.

## Results at a glance

| Acceptance check | Result |
|---|---|
| 1. Context before/after | Planner schema context per call: **Elastic 187,028 → 3,518 tokens (−98.1%)**, **DuckDB 289,297 → 4,236 (−98.5%)**, **Postgres 257,453 → 36,251 (−85.9%)**. Widest table rendered: 5,000 / 1,600 columns → **60**, plus a `<more_columns>` fold summary. Real-LLM planner input (uncached, first call): Elastic 187,976 → 4,508; Postgres 258,431 → 37,329. |
| 2. Used columns score higher | Real queries recorded 20 `ColumnStats` rows across 9 tables; every used column ranks into the rendered top columns ahead of unused ones. Thumbs down −0.200, thumbs up +0.200 on the exact columns the answer used; a failure-only column scores 0.175 vs 0.599 for a success-only one. |
| 3. No query-time growth | Statements per build constant (17 / 20) from 20 to 300,000 `column_stats` rows; the stats read is one index-bounded scan per rendered wide table (≤240 rows), so added work is capped at ~5 ms per wide table + ~4 ms query (deterministic); wall-clock medians within this VM's noise. Per-step write 24–52 ms, off the answer path. A constant ~25–45 ms over base on the 5,000-column agents. |
| 4. Column search works | Haiku 5.5 called `describe_tables(query=…, columns=["retry", …])` for a field folded out of context and answered exactly (Elastic 49.87 / 49.40 / 49.23; DuckDB 49.13 / 44.92 / 44.90 — equal to direct queries). Same on Postgres and DuckDB. |
| 5. Real LLM end-to-end | All runs on `claude-haiku-5-5` (already in the catalog at $0.10 / $0.50 per M tokens; `test_connection` 200). |
| 6. Tests | 88 unit + 13 e2e new tests pass on SQLite; the 13 e2e also pass on PostgreSQL 16. Three mutations of the code each fail the suite. |
| 7. Playwright demo | `media/pr/bow-215/`: Tables settings, context panels on all three sources, column search, thumbs up/down and reorder, before/after, Hebrew RTL, and a 93 s flow video (`bow215-flow.mp4`, `bow215-flow.gif`). |

## Fixtures (all synthetic, generic names)

`tools/agent/seed_wide_sources.py` builds, locally:

* **PostgreSQL 16** `wide_synth`: **2,000 tables** — 4 related `app_*` tables
  (PK/FK), **6 × `metrics_wide_00N` at 1,600 columns**, 1,990 narrow
  `tbl_NNNN` tables. Column families (`cpu_`, `mem_`, `disk_`, `net_`, `svc_`,
  `app_`, `kpi_`) and a few meaningful columns placed deep (85%) in the list
  (`net_retransmit_rate`, `svc_error_budget_pct`, `region_code`,
  `tenant_tier`). 300 rows each (`real` metrics: 1,600 doubles overflow the
  8 KB row).
  * **5,000-column views are impossible in PostgreSQL**, verified: a view over
    four joined 1,250-column tables fails with
    `ERROR: target lists can have at most 1664 entries`, and even 1,663
    columns fails with `ERROR: tables can have at most 1600 columns` (views
    are relations too). JSON-expanded views hit the same limits. So:
* **DuckDB** `wide_synth.duckdb` (a real SQL engine with no column cap):
  `events_wide_5000` and `telemetry_wide_5000` with **5,000 columns** each
  plus a `dim_region` table to join.
* **Elasticsearch 8.15.3** (single node, security off, localhost):
  `svc-events-wide` with **5,013 mapped fields** (nested objects `svc.*`,
  `host.*`, `net.*`, `process.*`, `user.*`, `geo.*`, a `nested` `items`
  array, `text` fields with `.keyword` multi-fields) and two daily indices
  `obs-logs-2026.11.20/21` with different field sets that BOW collapses into
  one `obs-logs-*` pattern table (2,205 columns, merged mappings).

`tools/agent/connect_wide_sources.py` connects them as three agents (Wide
Postgres, Wide DuckDB, Wide Elastic) through the public API with the existing
connectors and activates every table.

## Root cause (validated before the change)

* All columns rendered: `tables_schema_section.py` `render_table` /
  `_render_table_xml` wrote every column; the only reduction was the manual
  `excluded_columns` deny-list. Probe: 9,621 / 10,003 / 7,205 `<column>`
  entries per planner call.
* No column usage signal: the two real base-code runs stored
  `TableUsageEvent.columns = '[]'` (`select table_fqn, columns from
  table_usage_events` → `('svc-events-wide', '[]', 1)`,
  `('public.metrics_wide_001', '[]', 1)`): the data-model lineage regex finds
  nothing in an Elastic DSL or in unprefixed SQL.
* Failures never reached table stats either: `_handle_tool_output` returns
  early for a failed tool (`if _observation_failed(observation): return`), so
  `failure_count` stayed 0.
* `describe_tables` matched table names only and rendered every column of a
  matched table.

## What changed

**Step 1 — column usage (`ColumnStats`)**

* `app/utils/column_extraction.py`: pure extractors.
  * SQL via sqlglot scopes: aliases, unprefixed names (resolved against the
    catalog; ambiguous ones skipped), joins, `USING`, filters, `GROUP BY` /
    `HAVING` / `ORDER BY`, windows, CTEs, derived tables, correlated
    subqueries; `SELECT *` counts nothing; only catalog columns are reported.
  * Elastic DSL walker: `query` (bool, term/range/match/exists/nested,
    query_string fields), `aggs` (nested), `sort`, `_source`, `fields`,
    `collapse`, `runtime_mappings` scripts (`doc['x']`); `x.keyword` → `x`,
    `items.sku` → `items[].sku`; index patterns resolved to catalog tables;
    an embedded `sql` envelope goes to the SQL parser.
* `app/models/column_stats.py` + `alembic/versions/colstats01_column_stats.py`:
  one row per (table, column), unique on (`datasource_table_id`,
  `column_name`); usage / success / failure counts, weighted usage, feedback
  counts, `last_used_at`; `rank_weight` (net weighted usage) indexed with the
  table id for the read path; `ON DELETE CASCADE` from `datasource_tables`
  (re-sync deletes tables in bulk).
* `app/services/column_usage_service.py`: catalog resolution (names first, then
  columns of just the touched tables), **one batched `INSERT … ON CONFLICT DO
  UPDATE` per step**, feedback upsert, bounded batched read.
* `ProjectManager.emit_step_usage`: the step's executed queries + data-model
  lineage → validated columns per table → `TableUsageEvent.columns` (now real)
  → column stats for the newly recorded events only. **Dedupe is the existing
  once-per-(step, table) `TableUsageEvent`.**
* Failure path: `AgentV2._record_failed_step_usage` records a failed
  create_data whose query actually ran, as a failure (table and columns). A
  failure adds 0.25× of a success to weighted usage.
* Thumbs up/down: `CompletionFeedbackService` applies feedback to the columns
  the completion's steps used (one batched write).
* All writes stay off the answer path (the existing background write, drained
  before `completion.finished`; inline only on the SQLite single-writer).

**Step 2 — column cap per table**

* `app/ai/context/column_ranking.py`: score = 0.35·√(weighted usage)·recency
  (14-day decay) + 0.25·success rate + 0.2·net feedback (the table score's
  shape) + instruction references (`InstructionReference.column_name`) +
  0.2·structure (PK 1.0, FK 0.8, named timestamp 0.6, id-like 0.5). Columns with
  evidence lead; the remaining slots are a **round-robin sample across column
  families** (cold start included), so a 5,000-field index shows every family,
  not 60 `geo.client.*` attributes. A used `x` brings its `x.keyword` along.
* Budget: org setting `top_k_schema_columns` (default **60**, 0 = no cap,
  shown in AI settings → limits). Applied after `excluded_columns` and the
  per-user overlay filter.
* Render: top columns in full, then
  `<more_columns count="4940" shown="60" total="5000">svc.* (920), host.* (915), …
  Not listed here, but queryable: find them with describe_tables(query="…",
  columns=[…])</more_columns>`. Both render paths share one column renderer.
* Snapshot (`get_usage_snapshot`): per table `columns_shown` / `columns_total`,
  `column_selection_reason` (`all_columns` | `usage_ranked` |
  `structural_only`), signal counts, shown columns with usage, fold groups;
  table `selection_reason` is `cached` for cached relations.
* `create_data` / `inspect_data`: columns named in the planner's task are
  promoted for the code generator, so a column found by column search reaches
  the coder.
* Read cost: the builder loads stats only for the wide tables the planner
  render shows (top-k + cached; every matched table on a filtered build), in
  **one statement**: per table, its top `max(4·budget, 200)` rows in
  `ix_colstats_table_rank` order, glued with `UNION ALL`.

**Step 3 — column search in `describe_tables`**

* New optional `columns` input (names or patterns; substring, `*`/`?`
  wildcards matched at name-segment starts, or regex; against name and
  description). `query` is now optional (one of the two is required).
* With `query`: only matching columns, `showing K of N`
  (`<column_search matched="4" total="9">`). Without: every active table in
  scope that has a match, with just those columns. No filter: the same cap and
  fold, pointing at `columns`.
* Matches are ordered most specific first (a column hit by a narrow term leads
  columns hit only by a broad one), then by usage. `excluded_columns` are never
  returned (builder filter + a second filter in the search).
* Tool description and the planner prompt say unlisted columns exist and how
  to find them.

**UI**

* Agent Tables settings: a "Used" column; columns sorted most used first, ties
  by net thumbs.
* Context panel: "N of M columns" badge, the reason, "why these columns" (signal
  counts), columns in context with use counts and scores, folded groups.
* describe_tables tool card: column search terms in the header, "K of N columns
  match …" / "Top 60 of 5,000 columns", a Used column, sorted by usage.
* i18n: 35 keys added to all 10 catalogs with identical shape (en/es/he
  translated by hand, the seven lagging catalogs too); the sync counts are
  unchanged (es 31 / he 11 / others 814 missing — all pre-existing). The
  frontend locale sweep (`playwright.i18n.config.ts`, en/es/he and the rest)
  passes: 30 passed.

## Loop A — deterministic (no external services)

```bash
cd backend && export BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true
uv run pytest tests/unit/test_column_extraction.py tests/unit/test_column_ranking.py -q
uv run pytest tests/e2e/test_column_usage.py -m e2e --db=sqlite -q
# PostgreSQL leg (throwaway local database):
TEST_DATABASE_URL=postgresql://…/bow_e2e uv run pytest tests/e2e/test_column_usage.py -m e2e --db=external -q
```

Observed (final run): `88 passed` (unit), `13 passed` (e2e, SQLite),
`13 passed` (e2e, PostgreSQL 16, `--db=external`).

What they cover:

* extractor: 9 SQL shapes × 4 dialects (postgres, tsql, snowflake, generic),
  T-SQL brackets/`TOP`/hints, identifier case, qualified names, out-of-catalog
  tables, unparseable input; DSL query/aggs/sort/_source/fields/scripts,
  `.keyword` folding, nested paths, index patterns, runtime fields, ES SQL;
* ranking: usage beats unused, failure < success but > nothing, feedback both
  ways, recency, instruction refs and structure on cold start, family sampling,
  multi-field following, cap and fold at 61…5,000 columns, budget 0, both render
  paths, snapshot N of M and reasons, matcher semantics, segment wildcards,
  specificity order, coder promotion;
* e2e (real API, real DB): every column the executed SQL references is
  recorded; a step counts once however often it is emitted; failures count and
  weigh less; thumbs up/down through `POST /completions/{id}/feedback` hit
  exactly the answer's columns; the planner context caps and puts used columns
  first (budget 3 and 5 via the settings API); `excluded_columns` never reach
  the context even when most used; column search with and without `query`;
  column search never returns excluded columns; describe without a column
  filter caps and points at `columns`; the stats read is bounded per table and
  keeps the most used.

**Every test can fail** — mutations tried, each caught:

| Mutation | Result |
|---|---|
| `FAILURE_USAGE_WEIGHT = 1.0` (failures weigh like successes) | `test_failed_steps_count_as_failures_and_weigh_less` fails |
| ignore executed queries (lineage only, the old behaviour) | 6 e2e tests fail |
| drop the `excluded_columns` filter in the builder | `test_excluded_columns_never_reach_the_context_even_when_most_used` fails |

Regression check: every existing test file touching the changed code (92
files): **1,633 passed, 38 failed, 1 skipped, 0 errors** on the final code. The
38 failures are all in 4 OAuth/OBO files (`test_connection_oauth_flow.py`,
`test_oauth_connection_recovery.py`, `test_obo_background_database.py`,
`test_obo_stored_login_recovery.py`) and fail identically on the base commit
(verified in a worktree of `41ad448c`: 38 failed). One real regression was found
on the way and fixed: the ORM relationship `DataSourceTable.column_stats`
collided with the new API field of the same name (`MissingGreenlet` in
`DataSourceTableSchema.from_orm`); renamed to `column_stat_rows`.

## Loop B — live (real LLM, real sources, UI)

Stack: `tools/agent/boot_stack.sh --dev`, `seed_org.py`, `setup_haiku_llm.py`
with `HAIKU_MODEL_ID=claude-haiku-5-5` (key from env/file only, never printed),
`seed_wide_sources.py all`, `connect_wide_sources.py`. UI driver:
`tools/agent/bow215_ui.mjs` (logs in through the sign-in form; every prompt is
typed into the report composer).

### 1. Context before / after (same probe, same prompt, same DB)

`tools/agent/wide_context_probe.py` builds the schema exactly as the planner
does (`SchemaContextBuilder.build()` + `render_combined(top_k=10,
index_limit=1000)`); tokens from Anthropic `count_tokens` on `claude-haiku-5-5`.

| Agent | Chars before → after | Tokens before → after | `<column>`s rendered | Widest table |
|---|---|---|---|---|
| Wide Elastic (2 tables) | 416,696 → 7,833 | 187,028 → **3,518** | 7,205 → 120 | 5,000 → 60 |
| Wide DuckDB (3 tables) | 510,541 → 7,902 | 289,297 → **4,236** | 10,003 → 123 | 5,000 → 60 |
| Wide Postgres (2,000 tables) | 525,512 → 72,509 | 257,453 → **36,251** | 9,621 → 334 | 1,600 → 60 |

(The Postgres "after" still carries the 1,000-table name index and 10 top
tables, six of them 1,600-column tables at 60 each.)

Excerpts: `docs/feedback-loops/bow-215/before_*_excerpt.xml` and
`after_*_excerpt.xml` (the widest table's block, head and tail).
Snapshots: `before_summary.json`, `after_summary.json`.

Real-LLM token usage per run (`llm_usage_records`, uncached prompt tokens):

| Run | planner call 1 | code generation |
|---|---|---|
| Elastic, before | 187,976 | 144,433 |
| Elastic, after (column search) | 4,469 | 8,474 |
| Postgres, before | 258,431 | 40,175 |
| Postgres, after | 37,329 | 5,719 |

The trace header's "turn tokens" (683k before, 243–406k after) also counts cache
reads of the ~74k-token static system prompt on every call; the schema part is
what dropped.

### 2. Used columns score higher (real queries, success and failure, thumbs)

Real Haiku runs: 20 conversations (2 on the base commit, 18 on the branch) across the three agents, two deliberately
failing ones (`limit_code_retries=1`, "SUM(host_name) exactly as written",
"AVG(region_code)"). `ColumnStats` after the runs and the UI feedback (score =
usage part of the column score, computed from the stored rows):

| table | column | uses | success | failure | weighted usage | thumbs up | thumbs down | usage score w/o feedback | score with feedback | delta |
|---|---|---|---|---|---|---|---|---|---|---|
| svc-events-wide | event.outcome | 8 | 7 | 1 | 7.25 | 0 | 1 | 1.160 | 0.960 | -0.200 |
| svc-events-wide | event.kind | 5 | 4 | 1 | 4.25 | 0 | 0 | 0.921 | 0.921 | 0 |
| svc-events-wide | svc.request.tenant_tier | 4 | 4 | 0 | 4.00 | 1 | 1 | 0.949 | 0.949 | 0 |
| public.metrics_wide_001 | net_retransmit_rate | 3 | 3 | 0 | 3.00 | 1 | 0 | 0.856 | 1.056 | +0.200 |
| events_wide_5000 | checkout_abandon_rate | 3 | 3 | 0 | 3.00 | 0 | 0 | 0.856 | 0.856 | 0 |
| svc-events-wide | event.duration_ms | 2 | 2 | 0 | 2.00 | 1 | 0 | 0.744 | 0.944 | +0.200 |
| public.metrics_wide_001 | host_name | 2 | 1 | 1 | 1.25 | 1 | 0 | 0.516 | 0.716 | +0.200 |
| public.metrics_wide_001 | region_code | 2 | 2 | 0 | 2.00 | 0 | 0 | 0.744 | 0.744 | 0 |
| events_wide_5000 | tenant_tier | 2 | 2 | 0 | 2.00 | 0 | 0 | 0.744 | 0.744 | 0 |
| svc-events-wide | svc.request.retry_budget_remaining | 1 | 1 | 0 | 1.00 | 0 | 0 | 0.599 | 0.599 | 0 |
| public.metrics_wide_002 | region_code | 1 | 1 | 0 | 1.00 | 0 | 0 | 0.599 | 0.599 | 0 |
| public.metrics_wide_002 | svc_error_budget_pct | 1 | 1 | 0 | 1.00 | 0 | 0 | 0.599 | 0.599 | 0 |
| events_wide_5000 | region_code | 1 | 1 | 0 | 1.00 | 0 | 0 | 0.599 | 0.599 | 0 |
| dim_region | region_code | 1 | 1 | 0 | 1.00 | 0 | 0 | 0.599 | 0.599 | 0 |
| dim_region | region_name | 1 | 1 | 0 | 1.00 | 0 | 0 | 0.599 | 0.599 | 0 |
| svc-events-wide | message | 1 | 1 | 0 | 1.00 | 0 | 0 | 0.599 | 0.599 | 0 |
| public.metrics_wide_003 | region_code | 1 | 0 | 1 | 0.25 | 0 | 0 | 0.175 | 0.175 | 0 |
| svc-events-wide | @timestamp | 1 | 1 | 0 | 1.00 | 0 | 0 | 0.600 | 0.600 | 0 |
| telemetry_wide_5000 | svc_error_budget_pct | 1 | 1 | 0 | 1.00 | 1 | 0 | 0.600 | 0.800 | +0.200 |
| telemetry_wide_5000 | tenant_tier | 1 | 1 | 0 | 1.00 | 1 | 0 | 0.600 | 0.800 | +0.200 |

* Thumbs down on the "tenant tier failures" answer: −0.200 on exactly its
  columns (`event.outcome`, `svc.request.tenant_tier`); thumbs up on two answers:
  +0.200 on theirs. In Tables settings `event.outcome` (used 4×) dropped below
  `svc.request.tenant_tier` (used 4×) — `after-tables-elastic-before-feedback.png`
  vs `after-tables-elastic-after-feedback.png`.
* Failure counts less: `metrics_wide_003.region_code` (1 failure) scores 0.175;
  a single success scores 0.599; `host_name` (1 success + 1 failure) has
  weighted usage 1.25, not 2.
* The used columns now lead the rendered columns (context panel
  `after-context-postgres.png`: `net_retransmit_rate (used 2×)`,
  `host_name (2×)`, `region_code (1×)`, then keys, then a family sample).

### 3. No query-time growth

`tools/agent/column_stats_perf.py` on copies of the stack DB, backend stopped,
base commit (`/tmp/claude-0/base` worktree) and this branch alternated over 3
rounds; medians of 7 cold builds per round, median of rounds. `column_stats`
seeded to 100k / 300k rows over every table of every agent (real column names;
past the ~70k real (table, column) pairs, rows for retired column names).

Medians across 3 rounds (each round's value in parentheses):

| Agent | Metric | Base, 20 rows | Base, 300k rows | Branch, 20 rows | Branch, 100k rows | Branch, 300k rows |
|---|---|---|---|---|---|---|
| Wide Postgres | schema build (ms) | 551.5 (552/494/602) | 477.5 (478/642/453) | 553.6 (544/614/554) | 575.4 (535/637/575) | 652.3 (598/664/652) |
| Wide Postgres | planner render (ms) | 17.2 (22/14/17) | 15.2 (15/27/15) | 16.4 (14/16/22) | 15.0 (15/17/15) | 15.8 (14/16/16) |
| Wide Postgres | planner prep, end to end (ms) | 530.6 (475/549/531) | 598.9 (475/603/599) | 529.2 (529/752/513) | 562.6 (546/591/563) | 632.7 (548/633/634) |
| Wide Postgres | SQL statements per build | – | – | 20 | 20 | 20 |
| Wide DuckDB | schema build (ms) | 67.6 (77/68/60) | 56.4 (54/56/60) | 94.8 (92/101/95) | 142.5 (100/142/147) | 103.0 (103/132/99) |
| Wide DuckDB | planner render (ms) | 10.6 (16/11/10) | 9.3 (9/9/9) | 7.6 (8/8/7) | 13.3 (7/13/14) | 7.5 (7/12/8) |
| Wide DuckDB | planner prep, end to end (ms) | 72.4 (72/72/105) | 70.1 (70/117/70) | 114.9 (129/115/112) | 166.3 (119/188/166) | 126.2 (126/153/122) |
| Wide DuckDB | SQL statements per build | – | – | 17 | 17 | 17 |
| Wide Elastic | schema build (ms) | 49.8 (50/47/63) | 64.7 (65/52/65) | 79.8 (80/104/73) | 131.1 (117/131/155) | 101.9 (102/100/111) |
| Wide Elastic | planner render (ms) | 6.9 (7/6/7) | 9.0 (10/6/9) | 4.6 (5/5/5) | 8.5 (6/8/9) | 5.4 (5/5/6) |
| Wide Elastic | planner prep, end to end (ms) | 69.8 (60/75/70) | 66.6 (84/67/59) | 94.3 (85/108/94) | 152.7 (97/153/170) | 108.1 (138/108/108) |
| Wide Elastic | SQL statements per build | – | – | 17 | 17 | 17 |

Reading it honestly:

* The SQL work is constant: 17 / 20 statements per build at every size, and
  the stats read is one bounded index scan per rendered wide table.
* The wall-clock medians are noisy on this 4-core VM (round-to-round spread up
  to ±70 ms on Postgres, ±30 ms on the small agents; even the base commit reads
  551 vs 477 ms on two copies it treats identically). Within that noise:
  DuckDB / Elastic at 300k rows (103 / 102 ms) are close to 20 rows (95 / 80 ms);
  the 100k copy reads higher (142 / 131 ms) because nearly all of each wide
  table's top-240 stats rows are live columns there (more columns to score),
  while at 300k many are retired names that match nothing. Postgres reads
  554 → 575 → 652 ms; a trend of that size cannot be separated from noise here,
  so the deterministic bound below is the claim.
* Deterministic bound (one 5,000-column table, an ad-hoc harness on the 100k
  copy, medians of 15): stats read 4.1 ms (240 rows via the index);
  `rank_table_columns` 8.5 ms with no stats, 13.2 ms with the full 240 stats
  rows. The per-table limit (`max(4·budget, 200)` = 240 rows) caps this, so the
  added work is at most ~5 ms per rendered wide table plus the shared ~4 ms
  query, whatever `column_stats` holds (Postgres renders 6 such tables:
  ≤ ~35 ms).
* Against the base commit the branch costs a constant ~25–45 ms per build on
  the two 5,000-column agents (ranking 5,000 columns, the snapshot). It is paid
  once per run start (the context hub caches the built schema for 5 minutes)
  and buys a 98% smaller schema on every planner call (a turn's planner calls
  went from ~188k to ~4.5k uncached input tokens each).

Per-step stats write (catalog resolution + extraction + one batched upsert, a
representative query):

| Agent | per-step write, 20 rows | 100k rows | 300k rows | columns written per step |
|---|---|---|---|---|
| Wide Postgres | 46.8 ms | 47.1 ms | 51.5 ms | 5 |
| Wide DuckDB | 24.1 ms | 29.3 ms | 26.0 ms | 4 |
| Wide Elastic | 24.0 ms | 29.9 ms | 30.3 ms | 3 |

Flat in the stats size. It runs in the background write task, not on the
answer path.

Iteration that got here (kept for the record): the first version loaded every
stats row of a rendered wide table as an ORM entity — build grew 485 → 1,544 ms
(Postgres) at 66k rows. Plain rows plus a per-table limit via a window function
still sorted each table's whole history (DuckDB 92 → 304 ms at 300k). The final
`rank_weight` index + `UNION ALL` of per-table `ORDER BY … LIMIT` branches reads
a bounded index range per table.

### 4. Column search finds folded columns (and the answer is right)

| Agent | Question | describe_tables call | Answer vs ground truth |
|---|---|---|---|
| Elastic | "average remaining retry budget per event kind" | `query="svc-events-wide", columns=["retry","kind","budget","event.*"]` | request 49.87, job 49.40, alert 49.23 — ES `avg` agg: 49.87 / 49.4 / 49.23 |
| DuckDB (video) | "average error budget percentage per tenant tier in telemetry_wide_5000" | `columns` search → `svc_error_budget_pct` | bronze 49.13, silver 44.92, gold 44.90 — DuckDB: 49.13 / 44.92 / 44.90 |
| Postgres | "5 hosts with the highest average network retransmit rate" | 2 searches (see below) | matches `GROUP BY host_name` on `net_retransmit_rate` |
| DuckDB | "checkout abandon rate per region name" | 2 searches, then join to `dim_region` | matches the join |

The Postgres run exposed a search weakness that is now fixed: its first search
`["retrans*","tcp*","net_*"]` returned 200 of ~240 `net_*` columns and folded
`net_retransmit_rate` away, and `retrans*` only matched at the start of a name.
Wildcards now match at name-segment starts and narrow matches lead broad ones
(`test_narrow_matches_lead_broad_companion_patterns`,
`test_wildcards_match_at_name_segment_boundaries`).

### 7. Playwright evidence (`media/pr/bow-215/`, deviceScaleFactor 2)

| | |
|---|---|
| Before: Tables settings, 5,000 columns, alphabetical, no usage | `before-tables-elastic.png` |
| Before: context panel, "5000 columns (top_k_score)", 683k-token turn | `before-context-elastic.png`, `before-context-postgres.png` |
| After: Tables settings sorted by usage with counts | `after-tables-elastic-before-feedback.png` |
| Thumbs down dialog and the reorder it causes | `feedback-down-es-tier-failures.png`, `after-tables-elastic-after-feedback.png` |
| After: context panel "60 of 5,000 / 1,600 columns", reason, used columns, fold | `after-context-elastic.png`, `after-context-elastic-fold.png`, `after-context-postgres.png`, `after-context-duckdb.png` |
| Column search answers and the describe_tables card ("5 of 5,000 columns match …", Used column) | `es-colsearch-answer.png`, `after-describe-tables-card.png`, `pg-retransmit-answer.png`, `duck-abandon-answer.png` |
| A deliberately failing query (recorded as a failure) | `pg-fail-sum2-answer.png` |
| Hebrew (RTL) | `he-context-postgres.png`, `he-tables-elastic.png` |
| Flow video: Tables → live chat with column search → context panel → thumbs up → Tables re-sorted | `bow215-flow.mp4` (93 s), `bow215-flow.gif` |

## Still imperfect (stated plainly)

* PostgreSQL cannot hold a 5,000-column relation; the 5,000-column SQL tables
  are in DuckDB. Postgres is at its 1,600-column maximum.
* Usage shows up in the planner context after the context hub's 5-minute schema
  cache expires (same as table usage).
* `inspect_data` usage is still not recorded (it calls the emitter with no step,
  which never worked for tables either); only `create_data` steps feed stats.
* A failed create_data whose query ran but whose pandas code failed (e.g. a
  KeyError) also counts as a column failure — the step failed, but the query
  itself did not.
* Table feedback counts once per step, column feedback once per completion, so
  the two counters differ on multi-step answers (both shown in Tables settings).
* The wide-table columns panel in Tables settings still renders all 5,000 rows
  (pre-existing; now sorted by usage).
* "Name"/"Type" headers in that panel and most of the context panel's older
  labels were English before this change and still are; every string added here
  is localized.
* The 7 lagging locale catalogs got machine-quality translations for the new
  keys, written by the agent, not reviewed by native speakers.
