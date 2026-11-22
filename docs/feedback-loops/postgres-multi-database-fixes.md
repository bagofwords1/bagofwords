# Feedback Loop — PostgreSQL (multi-database): merge blockers

Follow-up to #1808 (one connection over many single-tenant Postgres databases,
unified through an embedded DuckDB). Three things stood between it and a merge:

1. It changed the table check every connector shares, and refused queries on
   other connectors that ran on `main`.
2. It had to wait for #1741, because a query the shared check misreads could
   reach any tenant database through the connector's engine.
3. The first query after every engine build paid tenants x tables round trips.

All three were reproduced first, then fixed, then re-run, against a real
Postgres 16 server and through the product UI with Claude Haiku 5.5.

## Sandbox

- Postgres 16 from the container's packages, a throwaway cluster on
  `127.0.0.1:55432`, statement and connection logging on.
- 30 synthetic tenant databases (`tenant_001` … `tenant_030`), 40 tables each:
  `orders`, `customers`, `salaries` and filler tables. Every fifth tenant lacks
  a column a later migration added, so the union has to bind across versions.
  Seeder: `seed_tenants.py` in the session scratch directory; nothing real.
- A second clean cluster on `:55433` for the PR's own integration tests, run
  with `BOW_TEST_MULTI_PG=127.0.0.1:55433:<user>:<password>` (new, for machines
  without Docker).
- BOW from `tools/agent/boot_stack.sh --dev` with `BOW_ENCRYPTION_KEY` pinned,
  Claude Haiku 5.5 as the org's model.

## 1. The shared table check

`table_scope.py` gained a rule that every `LATERAL` / `APPLY` source must be on
an allow-list. It applied to every connector with a table selection. Run with
the repository's own parser:

| Connector | Query shape | `main` | #1808 | This branch |
|---|---|---|---|---|
| SQL Server | `CROSS APPLY o.xml_col.nodes('/a')` | runs | refused | runs |
| Spark | `LATERAL VIEW inline_outer(o.items)` | runs | refused | runs |
| Hive | `LATERAL VIEW parse_url_tuple(o.url, 'HOST')` | runs | refused | runs |
| DuckDB | `LATERAL json_tree(o.j)` | runs | refused | runs |

The rule is removed from the shared check. Its stricter intent is kept for this
connector only, applied with DuckDB's own parser (below). The PR's other shared
additions stay: the wider denied-function list (`postgres_*`, `read_duckdb`) and
extra row functions on the safe list. The seven shared-test cases that asserted
the removed rule were added by #1808 and never existed on `main`.

## 2. The dependency on #1741

The connector's engine attaches every tenant database under one login. Its
check read SQL with sqlglot. A quoting form that sqlglot and DuckDB read
differently was found and run end to end; its exact text is in the regression
tests, not here. With an agent given only `public.orders`:

| Run | On #1808 as it stands | On this branch |
|---|---|---|
| Plain read of an unselected table | refused | refused |
| Same table hidden by the quoting form (client call) | **ran, returned the table's total** | refused |
| Same, in the UI: admin pastes it into a result's code editor and runs it | reached DuckDB; its error listed the tenant aliases | refused before DuckDB |
| Same form naming a tenant database directly (UI) | **ran, returned `salaries` from one tenant's raw database** | refused |

The fix, `backend/app/data_sources/clients/postgresql_multi_guard.py`: the
connector asks DuckDB's parser what the query is (`json_serialize_sql`, in a
separate engine with nothing attached; the query is bound as a parameter and
never run) and checks that reading. The sqlglot checks still run too; a query
must pass both. With this, the connector does not need #1741 to merge.

## 3. Engine build cost

Counted in the Postgres server's own log, 30 tenants x 40 tables, same query,
same result (30 groups, 10,650 orders):

| | Statements on the server | Time (local server) |
|---|---|---|
| First query on #1808 | 7,650 | 2.2 s |
| First query on this branch | 540 | 0.65 s |
| Later queries (both) | 93–180 | ~0.02 s |

Most of the 7,650 were connection-pool checks (`SELECT 1`, `DISCARD ALL`),
about six per tenant per table: the engine created every indexed view and
probed every tenant before each. Measured alternatives:

| Approach | Statements | Time |
|---|---|---|
| All 40 views, probe each tenant | 7,470 | 1.6 s |
| Attach only | 210 | 0.15 s |
| One view, probed per tenant | 240 | 0.47 s |
| One view, from the index, no probe | 150 | 0.38 s |

Locally each statement costs ~0.3 ms; against a remote server at 1–2 ms per
round trip, the old build at 200 tenants x 300 tables would take minutes.

Changed: views are created when a query first reads them, over the tenants the
index lists, in one statement; tenants are probed only if that fails. A healthy
engine now expires ten minutes after its last use instead of after it was built.

Drift, against the live tenants:

| Case | Result |
|---|---|
| Column missing on 6 tenants (older migration) | union binds; those rows read NULL |
| Table dropped on one tenant before the view was built | that tenant left out, 29 served |
| Table dropped on another after the view was built | existing retry rebuilds, 28 served |
| `SELECT` revoked on one tenant | after review: that tenant is left out of the view in place, the query answers from the rest, and the engine is marked degraded so a restored grant returns within 30 s (on #1808 the view failed until reindex: a rebuild alone puts the tenant back, since creating a view does not check `SELECT`) |

## In the product UI

- **Add connection → PostgreSQL (multi-database)**, `tenant_*`: connected and
  indexed 40 unified tables in 1 s. `media/pr/postgres-multi-fixes/connection-indexed.png`.
- Agent with only `public.orders` and `public.customers` enabled:
  - "orders and revenue per tenant, top 10": correct, checked against Postgres
    (`tenant_030` 500 orders / 125,000.00; `tenant_029` 490 / 122,160.00;
    `tenant_023` 430 / 107,820.00).
  - "revenue by customer segment, joining orders to customers": the agent's
    join included `o.tenant_db = c.tenant_db` as the connector instructs.
  - "total of public.salaries for tenant_001": the agent said the table is not
    available to it.

## After review

| Finding | Change | Test (fails before, passes after) |
|---|---|---|
| `DECIMAL` results were rounded through float64 (`9007199254740993.01` came back `…994.0`) | results go through Arrow, `DECIMAL` columns as exact `Decimal` (as the single-database PostgreSQL connector returns them); other column types unchanged, duplicate names kept | `test_decimals_come_back_exact` |
| `Amount` in one tenant and `amount` in another were listed as two columns but merged into one by `UNION ALL BY NAME` | indexing leaves the table out and reports it ("column names collide by case"), as it already does for table names | `test_column_names_that_differ_only_by_case_exclude_the_table` |
| Test connection missed a login that can read only materialized views | the probe also counts materialized views it can `SELECT`, as indexing does | `test_a_login_that_reads_only_materialized_views_passes_test_connection` |
| A revoked grant left a busy engine failing indefinitely | see the drift table above; plus a healthy engine is rebuilt at least hourly however busy | `test_revoked_select_on_one_tenant_leaves_it_out_without_a_rebuild`, `test_a_busy_healthy_engine_is_still_rebuilt_after_its_maximum_lifetime` |

A forged "permission denied" text in agent SQL leaves nobody out: the tenants
are left out only when Postgres itself says the login lacks `SELECT`
(`test_a_forged_permission_error_leaves_no_readable_tenant_out`).

## Review notes carried over from #1808

The notes left on #1808 (multi-tenant merge, DuckDB seal, concurrency, scale)
all still applied to this branch; all are addressed here.

| Note | Change | Test (fails before, passes after) |
|---|---|---|
| Benign type drift dropped tenants (`integer` vs `bigint` on a tie, `varchar(50)` vs `varchar(100)`) | indexing compares type families DuckDB merges without loss (integers, floats, text/varchar, numeric, timestamps by zone) and advertises the widest; `numeric` vs `text`, `integer` vs `double`, `timestamp` vs `timestamptz` still leave the minority out | `test_tenants_mid_migration_to_bigint_are_all_queried`, catalog unit tests |
| Engine seal re-implemented `lock_connection` without `python_enable_replacements=false` / `allow_unredacted_secrets=false` | both set; replacement scans are a per-cursor setting a later `SET` on the parent does not reach, so it is passed in the connection's config | `test_the_engine_is_sealed_like_every_other_duckdb_session` |
| Re-index during a build started a second build beside it | `invalidate_engines` leaves the build locks alone; the running build finishes under its lock, is marked stale and not reused, and the next caller builds afresh after it | `test_invalidating_during_a_build_does_not_start_a_second_build_beside_it` (+ the existing `test_build_interrupted_by_invalidate_is_not_cached`) |
| A new view on a cached engine held the build lock | views are created after the build lock is released, under the engine's own lock | `test_a_new_view_on_a_cached_engine_does_not_hold_up_other_queries` |
| Test connection and indexing read tenants one by one | up to 8 tenants at once (`TENANT_WORKERS`, also the connection bound), in the caller's context, progress reported in order | `test_test_connection_probes_tenants_in_parallel_within_a_bound` |
| Engine builds did not re-check column types | index stores each view's column types; a view is checked once when created (one cheap round) and, only if a column's type family moved, each tenant is described and the changed ones are left out and named | `test_a_tenant_whose_column_type_changed_after_indexing_is_left_out` |
| `INSTALL postgres` at query time, raw error | an installed extension is loaded first, download tried last; failure says what to set (`BOW_DUCKDB_EXTENSION_DIR`) and reaches the user as is | `test_postgres_extension_load_failure_names_the_fix`, `test_engine_build_failure_reaches_the_agent_as_a_duckdb_error` |
| 2 GB / 4 threads per engine, no bound on cached engines | at most 4 engines per process (`BOW_POSTGRES_MULTI_MAX_ENGINES`), least recently used dropped first | `test_cached_engines_are_capped_least_recently_used_first` |
| Same SQL parsed three times by sqlglot | parsed once; the shared checks take the parsed statements (`parse_for_refs`, `table_refs_of`, `refuse_unchecked_reads_of`) | existing guard suites |
| Build bookkeeping never pruned | keys with no engine, build or caller are pruned on expiry and on invalidate | `test_bookkeeping_of_unused_keys_is_pruned_when_engines_expire` |

Second review round:

| Finding | Change | Test (fails before, passes after) |
|---|---|---|
| **Blocker**: one tenant serving `DOUBLE` where the index has `BIGINT` passed the build-time check and rounded the other tenants' integers (`9007199254740993` → `…992.0`) | the check keeps exact numbers (integer, decimal) apart from approximate ones (float); Postgres bare `numeric`, which DuckDB serves as `DOUBLE` and indexing cannot tell from `numeric(p,s)`, fits either | `test_a_tenant_that_starts_serving_double_does_not_round_the_others`, `test_runtime_type_check_keeps_exact_and_approximate_numbers_apart` |
| The fallback after a missing table skipped the type check | the check runs after the fallback write too | `test_the_fallback_after_a_missing_table_still_checks_types` |
| `TIME WITH TIME ZONE` lost its offset through Arrow (on #1808 it was refused as unsupported) | results with that type are built from DuckDB's Python values, which keep the offset and exact decimals | `test_time_with_time_zone_keeps_its_offset` |

Measured on the same 30 tenants x 40 tables (local server):

| | Before these changes | After |
|---|---|---|
| Test connection | 0.24 s | 0.13 s |
| Indexing | 0.55 s | 0.24–0.31 s |
| First query (statements on the server) | 540 | 630 (+90: the type check, 3 pool checks per tenant) |
| Later queries | 180 | 180 |

The parallel gain grows with network latency; locally each round trip is ~0.3 ms.

## Tests

| Suite | Result |
|---|---|
| PR integration tests against real Postgres (`test_postgresql_multi_engine.py`, `test_postgresql_multi_client.py`) | 95 passed; 98 after review; 102 after the #1808 notes; 105 after the second review |
| New integration test (hidden table via quoting form, two shapes) on #1808's code | both fail ("did not raise") |
| PR unit tests + shared guard tests | pass (`test_table_scope_guard.py` 374) |
| `test_postgresql_multi_reading.py` (new, DuckDB-side check) | pass |
| Related unit subset (`-k table_scope/sql_guard/guard/code_execution/postgres/duckdb/multi/…`) | 3,272 passed, 25 failed, 1 error; all environment-only (SharePoint proxy CA bundle; `libodbc.so.2` missing) |

Two integration tests changed on purpose: the TTL test now asserts expiry
after last use, and the description test passes again because a view whose
every tenant failed to attach is named without being built.

## Not covered

- A real RDS instance, TLS, IAM auth, hundreds of tenants.
- Postgres as BOW's application database (SQLite here).
- The per-query round of pool checks on every tenant (93 statements even for a
  query filtered to one tenant). Unchanged.
