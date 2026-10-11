# Feedback Loop — "SQL policy bypass in code execution sandbox" / "State-changing SQL execution allowed in /custom-queries/preview"

Two pentest findings (Business Logic Bypass, Medium). They come down to one claim:
nothing checks the final SQL that reaches a data source to make sure it is
read-only. Generated sandbox code can build a write at runtime, and a
custom-table definition is executed exactly as written.

## Root cause (validated)

- `backend/app/ai/code_execution/code_execution.py` — the only SQL check on
  sandbox code was the AST scan of string **literals** (`visit_Constant` /
  `visit_JoinedStr`). SQL assembled at runtime (`'DEL' + 'ETE'`,
  `bytes.fromhex(...)`) is never a literal. `validate_sql_query` existed but was
  never called. `execute_query` passed the final text straight to the client.
- `backend/app/services/custom_query_service.py` `preview` →
  `data_sources/fast/extractor.py` `estimate`/`preview` →
  `sources.py` `SqlAlchemySource.preview` runs `text(definition_sql)` with only
  an emptiness check. The same text feeds `EXPLAIN …` and every scheduled
  refresh.
- Observed: on SQLite, plain DML went through but was rolled back when the
  connection closed. **DDL took effect**: `DROP TABLE orders` through the
  preview endpoint dropped the table. Engines that auto-commit (MySQL DDL,
  Snowflake, BigQuery, ClickHouse) are worse.

## Loop A — deterministic reproduction (no external services)

```bash
cd backend && uv sync --frozen --extra dev && mkdir -p db
export TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db
uv run pytest tests/unit/test_sql_read_only_guard.py -q
```

The test runs the real `StreamingCodeExecutor` and the real `extractor` against a
real SQLite file, then counts rows. Before the fix (wiring reverted):

```
FAILED test_sandbox_runtime_built_writes_never_reach_the_database[dynamic DEL+ETE]
FAILED test_sandbox_runtime_built_writes_never_reach_the_database[hex DROP]
FAILED test_custom_table_definition_writes_never_reach_the_database[SELECT 1; DELETE FROM orders]
FAILED test_custom_table_definition_writes_never_reach_the_database[DELETE FROM orders]
FAILED test_custom_table_definition_writes_never_reach_the_database[DROP TABLE orders]
5 failed, 3 passed
```

## Loop B — live HTTP, running backend

Boot the backend as `tools/agent/boot_stack.sh` does, then run
`tools/agent/seed_org.py`, then run `bash docs/feedback-loops/sql-read-only-guard-live.sh`. The script creates a SQLite connection to a five-row
`orders` file, turns on `enable_custom_queries`, and calls
`POST /api/connections/{id}/custom-queries/preview`.

| definition_sql | Before | After |
|---|---|---|
| `SELECT id, amount FROM orders ORDER BY id` | 200, 5 rows | 200, 5 rows |
| `SELECT 1; DELETE FROM orders` | 400 (driver: one statement) | 400 "Only a single read-only statement is allowed" |
| `DELETE FROM orders` | 400, **reached the DB** (rolled back) | 400 "Statement 'DELETE' is not allowed" |
| `DROP TABLE orders` | 400, **table dropped** (`no such table: orders`) | 400 "Statement 'DROP' is not allowed"; 5 rows remain |

Sandbox path, real executor and `SqliteClient`, payload
`bytes.fromhex('44524f…').decode()` → `DROP TABLE orders`:

```
BEFORE  orders rows after:  TABLE GONE -> no such table: orders
AFTER   orders rows after:  5
```

## The fix

- New `backend/app/ai/code_execution/sql_guard.py`. `ensure_read_only(client, query)`:
  - **Client with a known sqlglot dialect** (`CLIENT_DIALECTS`: Postgres,
    Redshift, MySQL/MariaDB, SQL Server, Fabric, Oracle, SQLite, Snowflake,
    BigQuery, Athena, Presto, Trino, ClickHouse, Databricks, Spark, DuckDB,
    Teradata, Druid): allow exactly one statement, which must be SELECT, a set
    operation, SHOW, DESCRIBE or EXPLAIN, or a read-only PRAGMA. Refuse anything
    that writes anywhere in the tree: a DELETE inside a CTE, `SELECT … INTO`,
    `COPY`, `EXEC`, or `EXPLAIN ANALYZE <write>`. If sqlglot can't parse the
    text, fall back to a keyword blocklist that ignores string literals and
    comments, so a parser gap never blocks a valid read.
  - **Splunk**: refuse SPL commands that write (`delete`, `outputlookup`,
    `collect`, `sendemail`, `script`, …).
  - **Every other client** (Salesforce, PostHog, Mongo, Qlik, Power BI, Sybase,
    …): unchanged.
- `code_execution.py` `execute_query`: runs the guard on the final query, after
  params are rendered, and raises `UnsafeSQLError`.
- `extractor.py` `_source(client, sql)`: runs the guard, which covers estimate,
  preview and refresh (create/update).
- New dependency `sqlglot>=27,<28` (pure Python).

After the fix: `251 passed`. Reads with `REPLACE()`, `status = 'deleted'`,
keywords inside comments, CTEs, window functions, `TOP`, `QUALIFY` and `SHOW`
pass in all 12 dialects tested.

## What this proves / regression notes

- A write built at runtime or saved as a custom table never reaches the source
  for any mapped dialect. Reads are unaffected.
- Not covered (defence in depth only; read-only DB accounts are still the real
  control): SELECTs that call side-effecting functions (`pg_terminate_backend`,
  `nextval`), clients with no dialect mapping, and writes sqlglot can't parse
  that also avoid every fallback keyword.
- A refused definition on **create** still persists the row with
  `last_refresh_status = "error"` (the refresh raises 400), the same as any
  failing definition today.
- If generated code catches the `UnsafeSQLError` itself, the run continues. The
  write still never happens, and the refusal is recorded in the query timings.
