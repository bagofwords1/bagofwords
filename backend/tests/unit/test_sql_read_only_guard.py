"""Read-only guard on SQL that reaches a data source.

Contract: whatever text finally reaches a SQL client — built at runtime by
sandbox code, or saved as a custom-table definition — runs only if it is a
single read statement. Writes are refused before the source sees them, reads
in every supported dialect still run, and non-SQL clients are untouched.

Born from the pentest findings "SQL policy bypass in code execution sandbox
via dynamic string construction" and "State-changing SQL execution allowed in
/custom-queries/preview".
"""

import sqlite3

import pytest

from app.ai.code_execution.code_execution import StreamingCodeExecutor, UnsafeSQLError
from app.ai.code_execution.sql_guard import (
    ReadOnlyViolation,
    check_sql,
    ensure_read_only,
)
from app.data_sources.clients.sqlite_client import SqliteClient
from app.data_sources.fast import extractor


DIALECTS = ["postgres", "mysql", "tsql", "oracle", "sqlite", "snowflake",
            "bigquery", "redshift", "trino", "clickhouse", "databricks", "duckdb"]

WRITES = [
    "DELETE FROM orders",
    "delete from orders where id = 1",
    "UPDATE orders SET amount = 0",
    "INSERT INTO orders (id) VALUES (9)",
    "DROP TABLE orders",
    "TRUNCATE TABLE orders",
    "ALTER TABLE orders ADD COLUMN x INT",
    "CREATE TABLE t2 AS SELECT * FROM orders",
    "SELECT 1; DROP TABLE orders",
    "SELECT * FROM orders; DELETE FROM orders",
    "SELECT 1; -- keep going\nDELETE FROM orders",
    "GRANT SELECT ON orders TO public",
]

READS = [
    "SELECT 1",
    "SELECT id, amount FROM orders WHERE status = 'deleted' ORDER BY amount DESC",
    "SELECT REPLACE(name, 'a', 'b') AS n FROM customers",
    "WITH big AS (SELECT * FROM orders WHERE amount > 100) SELECT COUNT(*) FROM big",
    "SELECT a FROM t UNION ALL SELECT b FROM u",
    "SELECT id, ROW_NUMBER() OVER (PARTITION BY c ORDER BY d) AS rn FROM t",
    "-- update the totals\nSELECT SUM(amount) FROM orders /* drop me */",
    "SELECT * FROM orders WHERE note = 'please DELETE FROM my list';",
    "SELECT * FROM orders LIMIT 5; -- sample",
    "SELECT * FROM orders;\n-- trailing note",
    "SELECT * FROM orders; /* trailing note */",
]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("sql", WRITES)
def test_writes_are_refused_in_every_dialect(dialect, sql):
    with pytest.raises(ReadOnlyViolation):
        check_sql(sql, dialect)


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("sql", READS)
def test_reads_pass_in_every_dialect(dialect, sql):
    check_sql(sql, dialect)


@pytest.mark.parametrize("sql,dialect", [
    ("WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d", "postgres"),
    ("SELECT * INTO orders_copy FROM orders", "postgres"),
    ("SELECT * INTO #tmp FROM orders", "tsql"),
    ("COPY orders TO PROGRAM 'id'", "postgres"),
    ("EXPLAIN ANALYZE DELETE FROM orders", "postgres"),
    ("EXEC sp_configure 'xp_cmdshell', 1", "tsql"),
    ("MERGE INTO t USING s ON t.a = s.a WHEN MATCHED THEN DELETE", "snowflake"),
    ("PRAGMA journal_mode = DELETE", "sqlite"),
    ("ATTACH DATABASE '/tmp/x.db' AS x", "sqlite"),
    ("VACUUM", "postgres"),
])
def test_dialect_specific_writes_are_refused(sql, dialect):
    with pytest.raises(ReadOnlyViolation):
        check_sql(sql, dialect)


@pytest.mark.parametrize("sql,dialect", [
    ("SHOW TABLES", "snowflake"),
    ("EXPLAIN SELECT * FROM orders", "postgres"),
    ("PRAGMA table_info(orders)", "sqlite"),
    ("SELECT TOP 5 * FROM orders", "tsql"),
])
def test_dialect_specific_reads_pass(sql, dialect):
    check_sql(sql, dialect)


def test_non_sql_clients_are_not_checked():
    class SalesforceClient:  # SOQL / HogQL / etc. go through unchanged
        pass

    ensure_read_only(SalesforceClient(), "DELETE whatever this API means")


def test_splunk_writing_commands_are_refused():
    from app.data_sources.clients.splunk_client import SplunkClient

    client = object.__new__(SplunkClient)
    ensure_read_only(client, "search index=web | stats count by host")
    for spl in ("search index=web | delete",
                "search * | outputlookup evil.csv",
                '{"spl": "search * | collect index=main"}'):
        with pytest.raises(ReadOnlyViolation):
            ensure_read_only(client, spl)


# ---------------------------------------------------------------------------
# Through the real paths, against a real database file
# ---------------------------------------------------------------------------

@pytest.fixture
def orders_db(tmp_path):
    path = tmp_path / "shop.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL)")
    con.executemany("INSERT INTO orders VALUES (?, ?)", [(i, i * 10.0) for i in range(1, 6)])
    con.commit()
    con.close()
    return path


def _row_count(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    finally:
        con.close()


# Generated code that never contains a SQL write as a literal: the AST scan
# has nothing to match, so only the runtime check can stop it.
DYNAMIC_DELETE_CODE = (
    "def generate_df(ds_clients, excel_files):\n"
    "    verb = 'DEL' + 'ETE'\n"
    "    ds_clients['shop'].execute_query(verb + ' FROM orders')\n"
    "    return ds_clients['shop'].execute_query('SELECT COUNT(*) AS n FROM orders')\n"
)

HEX_DROP_CODE = (
    "def generate_df(ds_clients, excel_files):\n"
    "    sql = bytes.fromhex('44524f50205441424c45206f7264657273').decode()\n"
    "    return ds_clients['shop'].execute_query(sql)\n"
)

READ_CODE = (
    "def generate_df(ds_clients, excel_files):\n"
    "    return ds_clients['shop'].execute_query('SELECT SUM(amount) AS total FROM orders')\n"
)


@pytest.mark.parametrize("code", [DYNAMIC_DELETE_CODE, HEX_DROP_CODE])
def test_sandbox_runtime_built_writes_never_reach_the_database(orders_db, code):
    client = SqliteClient(database=str(orders_db))
    with pytest.raises(UnsafeSQLError):
        StreamingCodeExecutor().execute_code(
            code=code, ds_clients={"shop": client}, excel_files=[]
        )
    assert _row_count(orders_db) == 5


def test_sandbox_reads_still_run(orders_db):
    client = SqliteClient(database=str(orders_db))
    df, _, queries = StreamingCodeExecutor().execute_code(
        code=READ_CODE, ds_clients={"shop": client}, excel_files=[]
    )
    assert df["total"].iloc[0] == 150.0
    assert len(queries) == 1


@pytest.mark.parametrize("sql", [
    "SELECT 1; DELETE FROM orders",
    "DELETE FROM orders",
    "DROP TABLE orders",
])
def test_custom_table_definition_writes_never_reach_the_database(orders_db, sql):
    client = SqliteClient(database=str(orders_db))
    with pytest.raises(ReadOnlyViolation):
        extractor.estimate(client, sql)
    with pytest.raises(ReadOnlyViolation):
        extractor.preview(client, sql, 10)
    assert _row_count(orders_db) == 5


def test_custom_table_definition_reads_still_preview(orders_db):
    client = SqliteClient(database=str(orders_db))
    cols, rows = extractor.preview(client, "SELECT id, amount FROM orders ORDER BY id", 3)
    assert [c["name"] for c in cols] == ["id", "amount"]
    assert len(rows) == 3
