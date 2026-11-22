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
| `SELECT` revoked on one tenant | the view fails until the next rebuild — **same as on #1808**, already listed there as a known limitation |

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

## Tests

| Suite | Result |
|---|---|
| PR integration tests against real Postgres (`test_postgresql_multi_engine.py`, `test_postgresql_multi_client.py`) | 95 passed |
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
- The revoked-grant case above.
