# Several Snowflake databases on one connection

## Problem

A Snowflake connection held exactly one `database`. Customers with data in
several databases on the same account had to add one connection per database,
and the agent was told tables under different connections "CANNOT join", so it
pulled each side into pandas and merged in memory. Snowflake itself joins
across databases of one account in a single query.

## Change

- **Config.** `database` takes a comma-separated list; the first entry is the
  session's default database. `schema` entries may be `DB.SCHEMA`; a plain
  `SCHEMA` belongs to the default database, and a database with no entry is
  read in full.
- **Naming.** One database: tables stay `SCHEMA.TABLE` (no change for
  existing connections). Two or more: every table is `DATABASE.SCHEMA.TABLE`.
  `metadata_json` now records `database` next to `schema`.
- **Discovery** (`snowflake_client.py`) reads each database's own
  `INFORMATION_SCHEMA`, semantic views and foreign keys.
- **Rename in place.** Adding a second database renames `SCHEMA.TABLE` to
  `DEFAULT_DB.SCHEMA.TABLE` (removing it renames back) on the same
  `connection_tables` row: `SnowflakeClient.table_name_aliases` +
  `ConnectionService.refresh_schema`. Agent selections, instructions and stats
  follow, since they point at row ids. Per-user visibility rows, which store
  the name, are renamed too.
- **Table selection check** (`table_scope.py`). A two-part name, stored or
  written by the agent, is read as `default_db.schema.table`, the way Snowflake
  resolves it. So enabled tables of every listed database can be joined, and a
  same-named table in another database no longer passes for an enabled one.
  Enforced for multi-database connections. On single-database connections the
  old lenient match is still allowed but logged
  (`table_scope: allowed ... only because it matches ...`), so saved queries
  keep running while the log shows whether anyone relies on it.
- **Prompts.** Planner and coder: tables of one connection can be joined in one
  query even across database prefixes. The client description (coder) lists
  the databases and says to write full names and join in SQL.
- **Connection test** names any listed database the role cannot read.
- **Preview** quotes `"DB"."SCHEMA"."TABLE"`.
- **Foreign keys (also fixes single-database connections).** The dialect
  returns lower-cased table names, so Snowflake foreign keys never matched the
  upper-case tables and were silently dropped. They are now matched
  case-insensitively and attach in every database.

## Verification

Sandbox: real backend + frontend, Snowflake replaced by a
[fakesnow](https://github.com/tekumara/fakesnow) server (DuckDB) at the network
boundary. The backend's own snowflake-connector and snowflake-sqlalchemy run
unchanged; only host/port/protocol are redirected.

- fakesnow needs its own venv (it pins `sqlglot~=30`, `duckdb~=2`): `uv venv`,
  `uv pip install "fakesnow[server]==0.12.1" "snowflake-connector-python[pandas]<4"`.
- Two shims in the fake server: `SHOW SEMANTIC VIEWS` returns an empty set
  (fakesnow lacks it; without it discovery is flagged incomplete), and
  `SHOW IMPORTED KEYS` answers in real Snowflake's column layout (fakesnow
  reports primary keys with no target).
- Seed: `DB_A.SALES.{ORDERS, ORDER_ITEMS, SECRETS}`,
  `DB_B.CRM.{CUSTOMERS, CONTACTS}`, and a decoy `DB_B.SALES.ORDERS` with one
  999999 order (same schema.table as the real orders).
- Backend started through a wrapper that patches `snowflake.connector.connect`
  to add `host=127.0.0.1, port=<fakesnow>, protocol=http`, with uvicorn
  `reload=False` (agent queries are brokered back to the parent process, so the
  patch covers them).

Results:

| Check | Result |
|---|---|
| UI: connection form shows the new Database/Schema help | yes |
| UI: connect with `DB_A, DB_B`, index | 6 tables, all `DB.SCHEMA.TABLE`, FK badges on ORDER_ITEMS and CONTACTS |
| UI: enable 4 tables, leave SECRETS and the decoy off | saved |
| Planner schema context | one `<connection>`, full names, FKs inside each database |
| Agent-style code through the real executor and the agent's clients: `DB_A.SALES.ORDERS JOIN DB_B.CRM.CUSTOMERS` | one SQL statement reached Snowflake; AMER 95, EMEA 150 (correct) |
| Same, decoy `DB_B.SALES.ORDERS` / `SECRETS` / `DB_C...` | refused before reaching Snowflake |
| Remove DB_B, refresh | DB_A tables back to `SCHEMA.TABLE`, same row ids, selection kept |
| Add DB_B back, refresh | DB_A tables `DB_A.…`, same ids, selection kept; DB_B tables new and inactive |

Tests (all fail with the change removed, except the guards for unchanged
single-database behavior):

- `backend/tests/unit/test_snowflake_multi_database.py`: naming, per-database
  reads, schema routing, connection test, aliases, foreign keys.
- `backend/tests/unit/test_table_scope_databases.py`: joins, decoys,
  ambiguous names, legacy logging.
- `backend/tests/unit/test_table_preview_quoting.py`: 3-part preview.
- `backend/tests/e2e/test_snowflake_multi_database.py`: add/remove a database
  through the routes (rename in place, selection kept, per-user rows renamed,
  clients enforce the selection), connection test. Passes on SQLite and
  PostgreSQL 16.

**Known limitation:** the table picker's schema filter groups by schema name
only, so with several databases "SALES" lists both `DB_A.SALES.*` and
`DB_B.SALES.*`. Searching by the `DB_B.` prefix narrows to one database.

**Not done:** a live chat. The sandbox's Anthropic account had no credit, so
whether the model writes the single join instead of a pandas merge is
unverified. Re-run with credit: ask "total order amount per customer region"
against the seed above; expect one query joining `DB_A.SALES.ORDERS` and
`DB_B.CRM.CUSTOMERS`, and no 999999.
