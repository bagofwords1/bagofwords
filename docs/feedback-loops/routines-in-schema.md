# Feedback Loop — stored procedures and functions through `get_schemas()`

The claim under test: a SQL source's stored procedures and functions can ride
the normal schema catalog (`get_schemas()` → `ConnectionTable` →
`DataSourceTable` → schema context), the way Snowflake semantic views and
Power BI measures already do, instead of a parallel routines system with its
own model, selection column, routes, tools and UI panel (the first version of
PR #1506). And that an agent with that context answers routine-dependent
questions correctly, with `execute_query` running only read-only SELECTs.

## What changed (validated)

- `app/data_sources/routines.py` — catalog rows → one `Table` per routine
  name, marked `metadata_json["type"] == "routine"`. Parameters are columns
  (`role=parameter`); source is stored capped at 20k chars in
  `metadata_json["routine"]["definition"]`. Overloads (Postgres) and a
  same-named procedure + function (MySQL) collapse into one entry with every
  signature. A routine named like a table (legal in Postgres) is renamed
  `name()`. Extension-owned functions (pgcrypto…) are skipped.
- `postgresql_client.py`, `mssql_client.py`, `mysql_client.py`,
  `mariadb_client.py` — `get_schemas()` = tables, then `with_routines(...)`.
  A failing routine read logs and returns the tables; no tables → no
  routines (otherwise an authoritative reindex after a failed table crawl
  would read "every table dropped" and prune them).
- `tables_schema_section.py` — a routine renders as
  `<routine name kind><signature/></routine>`, never `<table>`, plus one
  `<routines_note>` per agent ("not tables; never EXEC/CALL; read the source
  with describe_tables and apply its logic in a SELECT; functions may be
  called inside a SELECT"). The standing context carries signatures only;
  `render_combined(include_routine_definitions=True)` (describe_tables) adds
  `<definition>`, or `available="false"` when the account can't see it.
- `describe_tables.py` — passes that flag; never "thin-samples" a routine.
- UI: `TablesSelector.vue` badges routines (procedure / function), shows
  signature + parameters + source instead of column-visibility toggles, and
  keeps them out of the relationship diagram; `KnowledgeExplorer.vue` gives
  them their own tree icon. Strings in all 10 catalogs.

## Loop A — deterministic (no external services)

```bash
cd backend
export BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true
uv run pytest -q tests/unit/test_routine_schema_entries.py            # 18 passed
uv run pytest -q tests/e2e/test_routines_in_schema.py -m e2e          # 4 passed
```

The e2e test drives the real API/services with only the DB driver stubbed:
routines are indexed next to unchanged tables, the schema API returns kind,
signature and source, a failing routine read keeps the tables, a routine
dropped upstream is pruned on reindex, and the agent's schema context lists
routines by signature (no source) while describe mode adds the source.

Mutation check (rule 6): forcing `include_definition=True` in the standing
render, and returning bare tables from `PostgresqlClient.get_schemas`,
turned 4 of these tests red for the right reasons.

## Loop B — real engines (Docker)

```bash
uv run pytest -q tests/integrations/test_routine_discovery.py   # needs Docker
```

Observed: Postgres 4/4 and SQL Server 3/3 passed against containers
(overloads, the name clash, pgcrypto excluded, schema filter honored,
`dbo.[load daily sales]` resolves, OUT params, an account without VIEW
DEFINITION sees the function flagged `definition_available=False` and never
sees the ungranted procedure). The MySQL class could not pull `mysql:8`
(Docker Hub 429 in the sandbox); the same assertions were checked by hand
against MariaDB 10.11 with both `MysqlClient` and `MariadbClient`: one `net`
entry with two signatures and no cross-merged parameters; a non-definer
account gets `definition_available=False`.

## Loop C — live agent (real LLM, Claude Haiku 5.5)

Stack: `tools/agent/boot_stack.sh --dev` (export a stable
`BOW_ENCRYPTION_KEY` first — a hot reload otherwise generates a new key and
stored credentials stop decrypting), Postgres 16 on :5433 seeded from
`routines-in-schema/seed_pg.sql`, SQL Server 2022 on :1433 from
`routines-in-schema/seed_mssql.sql`, then `routines-in-schema/drive.py seed`
and `drive.py ask …` (key from `ANTHROPIC_API_KEY`).

The fixtures put the business rules only in the routines: net revenue =
gross − discount − refunds, complete orders, no test customers; fiscal year
starts Feb 1 (`fn_fiscal_quarter`). FY2026-Q1 net revenue is **4,050**
(gross sum 12,500; a calendar-quarter guess 2,600).

| Agent / question | `main` (no routines) | this branch |
|---|---|---|
| Postgres: "What was our net revenue in fiscal Q1 of FY2026?" | stops to ask the user which fiscal calendar and net-revenue formula to use | `describe_tables` on the function + procedure → SQL calls `sales.fn_fiscal_quarter(o.order_date) = 'FY2026-Q1'` with the procedure's filters → **$4,050.00, 3 orders** |
| SQL Server: same question | assumes a calendar fiscal year → **$2,600** (wrong) | — |
| SQL Server: "Run usp_load_net_revenue for Feb–Apr 2026…" | — | "it's a stored procedure and I can only run read-only SELECTs, so I'll read its source and reproduce its logic" → **$4,050.00** |
| SQL Server: "Show net revenue by fiscal quarter." | — | `dbo.fn_fiscal_quarter(o.order_date)` in the SELECT → FY2026-Q1 4,050 (3), FY2026-Q2 900 (1) — both correct |
| SQL Server, login without VIEW DEFINITION: "How is fiscal quarter computed?" | — | reads the signature, says the source is not readable because the account lacks VIEW DEFINITION |
| Postgres: "How many customers per region?" (control) | — | describes `sales.customers`, asks whether to exclude test accounts — routines don't intrude |

Lower layers checked: the generated code (`steps.code`) is a single SELECT
in every run (the read-only guard accepted each, including the function
calls); `context_snapshots.schemas_usage` lists the routines among the
tables the planner received; a reindex after `CREATE PROCEDURE` /
`DROP FUNCTION` upstream added the new routine (inactive, per the normal
new-table rule), pruned the dropped one, and kept the existing selection.

Observed model variance: in one of the two Postgres runs Haiku described the
fiscal shift backwards in prose ("Nov–Jan") while its SQL called the
function and returned the right 4,050 — the narrative, not the data, was
wrong.

## UI evidence

`media/pr/feat-index-routines/`: `before-tables-panel.png` (routines indexed
but indistinguishable: table icon, parameters shown as "columns" with
context toggles), `after-tables-panel.png`, `after-source-not-visible.png`
(restricted login), `after-tables-panel-he.png` (RTL).

## Regression notes

Full `tests/unit` run: 8,839 passed, 171 failed — all 171 are
pre-existing / environmental and reproduce identically on a clean `main`
checkout: 167 SharePoint client tests (sandbox proxy CA bundle) and 4
`test_artifact_precompile` tests (they only run when the vendored Babel is
present — `boot_stack.sh` downloads it — and fail on `main` the same way).
