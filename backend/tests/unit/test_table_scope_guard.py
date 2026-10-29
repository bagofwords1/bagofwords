"""An agent's table selection holds at query time, not only in its prompt.

Contract: when a SQL client carries the agent's table scope, a query runs only
if every table it reads is activated for the agent. A table that is turned off
("inactive") is refused wherever it appears in the query and however it is
spelled; anything whose target cannot be verified (metadata statements,
string-to-table functions, text the parser cannot read) is refused too. Active
tables keep working in every supported dialect, and clients without a scope or
without a SQL dialect are not checked.

Born from a report where an agent queried LAKEHOUSE.GOLD.GL_KNO2_* views that
were turned off for it: the selection only hid them from the schema, and the
connection's credentials could read them.
"""

import sqlite3

import pytest

from app.ai.code_execution.code_execution import StreamingCodeExecutor, TableAccessError
from app.ai.code_execution.table_scope import (
    TABLE_SCOPE_ATTR,
    TableScope,
    TableScopeViolation,
    check_table_scope,
    enforce_table_scope,
)
from app.data_sources.clients.sqlite_client import SqliteClient

DIALECTS = ["postgres", "mysql", "tsql", "snowflake", "bigquery", "redshift",
            "trino", "databricks", "duckdb", "oracle", "clickhouse"]

SCOPE = TableScope.from_names(
    active=["sales.orders", "sales.customers"],
    inactive=["sales.payroll", "hr.salaries"],
)

# Every way of reading an inactive table that must be refused.
HIDDEN_READS = [
    "SELECT * FROM sales.payroll",
    "SELECT * FROM warehouse.sales.payroll",
    "SELECT * FROM SALES.PAYROLL",
    "SELECT COUNT(*) FROM hr.salaries s",
    "SELECT o.id FROM sales.orders o JOIN sales.payroll p ON o.id = p.id",
    "SELECT * FROM sales.orders WHERE id IN (SELECT id FROM hr.salaries)",
    "WITH x AS (SELECT * FROM sales.payroll) SELECT * FROM x",
    "SELECT * FROM sales.orders UNION ALL SELECT * FROM sales.payroll",
    # A CTE named like the hidden table does not make the qualified name safe.
    "WITH payroll AS (SELECT 1 AS id) SELECT * FROM sales.payroll",
    # Unqualified, and an inactive table ends with that name: ambiguous.
    "SELECT * FROM payroll",
    # Not in the catalog at all (system views, other schemas).
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM finance.ledger",
]

ALLOWED_READS = [
    "SELECT * FROM sales.orders",
    "SELECT * FROM warehouse.sales.orders",
    "SELECT COUNT(*) FROM SALES.ORDERS",
    "SELECT o.id FROM sales.orders o JOIN sales.customers c ON o.id = c.id",
    "WITH big AS (SELECT * FROM sales.orders WHERE amount > 1) SELECT COUNT(*) FROM big",
    "SELECT * FROM orders",
    "SELECT 1",
]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("sql", HIDDEN_READS)
def test_inactive_tables_are_refused_in_every_dialect(dialect, sql):
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, dialect, SCOPE)


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("sql", ALLOWED_READS)
def test_active_tables_run_in_every_dialect(dialect, sql):
    check_table_scope(sql, dialect, SCOPE)


@pytest.mark.parametrize("sql,dialect", [
    ('SELECT * FROM "SALES"."PAYROLL"', "snowflake"),
    ("SELECT * FROM `sales`.`payroll`", "mysql"),
    ("SELECT * FROM [sales].[payroll]", "tsql"),
    ("DESCRIBE TABLE sales.payroll", "snowflake"),
    ("SELECT * FROM IDENTIFIER('sales.payroll')", "snowflake"),
    ("SELECT * FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))", "snowflake"),
    ("SELECT * FROM @my_stage/payroll.csv", "snowflake"),
    ("SELECT * FROM SEMANTIC_VIEW(sales.payroll DIMENSIONS a.b METRICS c.d)", "snowflake"),
    ("SELECT query_to_xml('select * from sales.payroll', true, true, '')", "postgres"),
    ("SELECT schema_to_xml('sales', true, true, '')", "postgres"),
    ("SELECT * FROM read_csv('/data/payroll.csv')", "duckdb"),
])
def test_quoted_names_and_indirect_reads_are_refused(sql, dialect):
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, dialect, SCOPE)


@pytest.mark.parametrize("sql,dialect", [
    ("DESCRIBE TABLE sales.orders", "snowflake"),
    ("SELECT f.value FROM sales.orders o, LATERAL FLATTEN(input => o.items) f", "snowflake"),
    ("SELECT seq4() FROM TABLE(GENERATOR(ROWCOUNT => 3))", "snowflake"),
    ("SELECT * FROM SEMANTIC_VIEW(sales.orders DIMENSIONS a.b METRICS c.d)", "snowflake"),
    ("SELECT * FROM generate_series(1, 3) g", "postgres"),
    ("SELECT * FROM sales.orders o, jsonb_array_elements(o.items) i", "postgres"),
    ("SELECT * FROM UNNEST([1, 2])", "bigquery"),
])
def test_dialect_features_over_active_tables_still_run(sql, dialect):
    check_table_scope(sql, dialect, SCOPE)


@pytest.mark.parametrize("dialect", ["snowflake", "postgres", "mysql"])
@pytest.mark.parametrize("sql", [
    "SHOW TABLES",
    "SHOW TABLES LIKE '%PAY%' IN SCHEMA warehouse.sales",
    "EXPLAIN SELECT * FROM sales.payroll",
])
def test_metadata_statements_are_refused(dialect, sql):
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, dialect, SCOPE)


@pytest.mark.parametrize("dialect", ["snowflake", "postgres"])
@pytest.mark.parametrize("sql", ["SELECT * FROM", "SELECT (1 FROM sales.orders", "SELECT 1 +"])
def test_unparseable_queries_fail_closed(dialect, sql):
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, dialect, SCOPE)


def test_refusal_names_the_table_not_the_rest_of_the_catalog():
    with pytest.raises(TableScopeViolation) as exc:
        check_table_scope("SELECT * FROM sales.payroll", "postgres", SCOPE)
    message = str(exc.value).upper()
    assert "SALES.PAYROLL" in message
    assert "HR.SALARIES" not in message


def test_a_name_active_anywhere_wins_over_an_inactive_duplicate():
    scope = TableScope.from_names(active=["sales.orders"], inactive=["sales.orders"])
    check_table_scope("SELECT * FROM sales.orders", "postgres", scope)


def test_empty_selection_refuses_every_table():
    scope = TableScope.from_names(active=[], inactive=["sales.orders"])
    with pytest.raises(TableScopeViolation):
        check_table_scope("SELECT * FROM sales.orders", "postgres", scope)
    check_table_scope("SELECT 1", "postgres", scope)


def test_clients_without_scope_or_sql_dialect_are_not_checked():
    class _Mongo:
        pass

    mongo = _Mongo()
    setattr(mongo, TABLE_SCOPE_ATTR, SCOPE)
    enforce_table_scope(mongo, "SELECT * FROM sales.payroll")

    unscoped = SqliteClient(database=":memory:")
    enforce_table_scope(unscoped, "SELECT * FROM payroll")


# ---------------------------------------------------------------------------
# Through the real code-execution path, against a real database file
# ---------------------------------------------------------------------------

@pytest.fixture
def clinic_db(tmp_path):
    path = tmp_path / "clinic.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, patient_id INTEGER)")
    con.execute("CREATE TABLE patients (id INTEGER PRIMARY KEY, name TEXT)")
    con.executemany("INSERT INTO visits VALUES (?, ?)", [(i, i % 3) for i in range(1, 7)])
    con.executemany("INSERT INTO patients VALUES (?, ?)", [(i, f"secret-{i}") for i in range(3)])
    con.commit()
    con.close()
    return path


def _scoped_client(path):
    client = SqliteClient(database=str(path))
    setattr(client, TABLE_SCOPE_ATTR, TableScope.from_names(active=["visits"], inactive=["patients"]))
    return client


HIDDEN_CODE = (
    "def generate_df(ds_clients, excel_files):\n"
    "    return ds_clients['clinic'].execute_query('SELECT name FROM patients')\n"
)

# Generated code that catches the refusal and answers from an allowed table
# instead: the run must still fail, not quietly return something else.
SWALLOWING_CODE = (
    "def generate_df(ds_clients, excel_files):\n"
    "    try:\n"
    "        return ds_clients['clinic'].execute_query('SELECT name FROM patients')\n"
    "    except Exception:\n"
    "        return ds_clients['clinic'].execute_query('SELECT COUNT(*) AS n FROM visits')\n"
)

BUILT_AT_RUNTIME_CODE = (
    "def generate_df(ds_clients, excel_files):\n"
    "    t = 'pat' + 'ients'\n"
    "    return ds_clients['clinic'].execute_query('SELECT * FROM ' + t)\n"
)

ALLOWED_CODE = (
    "def generate_df(ds_clients, excel_files):\n"
    "    return ds_clients['clinic'].execute_query('SELECT COUNT(*) AS n FROM visits')\n"
)


@pytest.mark.parametrize("code", [HIDDEN_CODE, BUILT_AT_RUNTIME_CODE])
def test_sandbox_code_cannot_read_an_inactive_table(clinic_db, code):
    with pytest.raises(TableAccessError):
        StreamingCodeExecutor().execute_code(
            code=code, ds_clients={"clinic": _scoped_client(clinic_db)}, excel_files=[]
        )


def test_sandbox_code_that_swallows_the_refusal_still_fails(clinic_db):
    with pytest.raises(Exception) as exc:
        StreamingCodeExecutor().execute_code(
            code=SWALLOWING_CODE, ds_clients={"clinic": _scoped_client(clinic_db)}, excel_files=[]
        )
    assert "not enabled for this agent" in str(exc.value)


def test_sandbox_code_reading_active_tables_still_runs(clinic_db):
    df, _, queries = StreamingCodeExecutor().execute_code(
        code=ALLOWED_CODE, ds_clients={"clinic": _scoped_client(clinic_db)}, excel_files=[]
    )
    assert df["n"].iloc[0] == 6
    assert len(queries) == 1
