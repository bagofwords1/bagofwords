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
from app.ai.code_execution.sql_guard import CLIENT_DIALECTS
from app.ai.code_execution.table_scope import (
    TABLE_SCOPE_ATTR,
    TableScope,
    TableScopeViolation,
    check_table_scope,
    enforce_table_scope,
    is_allowed,
    one_part_table_refs,
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
    ("SELECT * FROM read_duckdb('other.db', table_name => 'payroll')", "duckdb"),
    ("SELECT * FROM sales.orders o, LATERAL TABLE(main.histogram('sales.payroll', v)) h", "duckdb"),
    ("SELECT f.value FROM sales.orders o, LATERAL TABLE(unknown_fn(o.x)) f", "snowflake"),
])
def test_quoted_names_and_indirect_reads_are_refused(sql, dialect):
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, dialect, SCOPE)


@pytest.mark.parametrize("dialect", ["postgres", "redshift"])
@pytest.mark.parametrize("sql", [
    "SELECT * FROM ts_stat('SELECT to_tsvector(code) FROM sales.payroll')",
    "SELECT word FROM pg_catalog.ts_stat('SELECT v FROM sales.payroll') AS s",
    "SELECT \"ts_stat\"('SELECT v FROM sales.payroll') FROM sales.orders",
    "SELECT ts_rewrite(q, 'SELECT a, b FROM sales.payroll') FROM sales.orders",
    "SELECT pg_catalog.\"ts_rewrite\"(q, 'SELECT a, b FROM sales.payroll') FROM sales.orders",
])
def test_functions_that_run_sql_held_in_a_string_are_refused(sql, dialect):
    with pytest.raises(TableScopeViolation, match="TS_"):
        check_table_scope(sql, dialect, SCOPE)


# The same functions written as a field or a column.
@pytest.mark.parametrize("dialect", ["postgres", "redshift"])
@pytest.mark.parametrize("sql, name", [
    ("SELECT ('SELECT to_tsvector(code) FROM sales.payroll'::text).ts_stat", "TS_STAT"),
    ("SELECT ('SELECT v FROM sales.payroll'::text).\"ts_stat\" FROM sales.orders", "TS_STAT"),
    ("SELECT ('select v from sales.payroll'::text).dblink FROM sales.orders", "DBLINK"),
    ("SELECT ('/srv/data/payroll.csv'::text).pg_read_file FROM sales.orders", "PG_READ_FILE"),
    ("SELECT ('/srv/data'::text).pg_ls_dir FROM sales.orders", "PG_LS_DIR"),
    ("SELECT (o.id::oid).lo_get FROM sales.orders o", "LO_GET"),
    ("SELECT o.pg_read_binary_file FROM sales.orders o", "PG_READ_BINARY_FILE"),
])
def test_functions_called_as_a_field_are_refused(sql, name, dialect):
    with pytest.raises(TableScopeViolation, match=name):
        check_table_scope(sql, dialect, SCOPE)


@pytest.mark.parametrize("sql,dialect", [
    ("SELECT T.c.value('.', 'int') FROM sales.orders o CROSS APPLY o.xml_col.nodes('/a') AS T(c)", "tsql"),
    ("SELECT o.id, x.a FROM sales.orders o LATERAL VIEW inline_outer(o.items) x AS a, b", "spark"),
    ("SELECT o.id, h FROM sales.orders o LATERAL VIEW parse_url_tuple(o.url, 'HOST') t AS h", "hive"),
    ("SELECT o.id, j.key FROM sales.orders o, LATERAL json_tree(o.j) j", "duckdb"),
])
def test_built_ins_beside_an_enabled_table_still_run(sql, dialect):
    """Built-ins that read no table still run beside an enabled table: the
    nodes() method of an xml column on SQL Server, the Spark and Hive
    generators under LATERAL VIEW, and DuckDB's json_tree. (The
    multi-database Postgres connector applies its own rule with its engine's
    parser: test_postgresql_multi_reading.py.)"""
    check_table_scope(sql, dialect, SCOPE)


@pytest.mark.parametrize("sql,dialect", [
    ("DESCRIBE TABLE sales.orders", "snowflake"),
    ("SELECT f.value FROM sales.orders o, LATERAL FLATTEN(input => o.items) f", "snowflake"),
    ("SELECT f.value FROM sales.orders o, LATERAL TABLE(FLATTEN(o.items)) f", "snowflake"),
    ("SELECT seq4() FROM TABLE(GENERATOR(ROWCOUNT => 3))", "snowflake"),
    ("SELECT * FROM SEMANTIC_VIEW(sales.orders DIMENSIONS a.b METRICS c.d)", "snowflake"),
    ("SELECT * FROM generate_series(1, 3) g", "postgres"),
    ("SELECT * FROM sales.orders o, jsonb_array_elements(o.items) i", "postgres"),
    ("SELECT o.lo_price, lo_bound, o.url, file FROM sales.orders o", "postgres"),
    ("SELECT o.lo_price, o.url FROM sales.orders o", "redshift"),
    ("SELECT * FROM UNNEST([1, 2])", "bigquery"),
    ("SELECT * FROM sales.orders o, LATERAL jsonb_array_elements(o.items) i", "postgres"),
    ("SELECT * FROM sales.orders o, LATERAL (SELECT * FROM sales.customers c WHERE c.id = o.id) x", "postgres"),
    ("SELECT * FROM sales.orders o, LATERAL unnest([1]) u", "duckdb"),
    ("SELECT * FROM sales.orders t LATERAL VIEW json_tuple(t.j, 'a') v AS a", "spark"),
    ("SELECT * FROM sales.orders t LATERAL VIEW explode(t.arr) v AS a", "spark"),
    ("SELECT * FROM sales.orders t LATERAL VIEW stack(2, 1, 2) v AS a", "spark"),
    ("SELECT * FROM sales.orders t, LATERAL regexp_matches(t.s, 'a') m", "postgres"),
    ("SELECT * FROM sales.orders t OUTER APPLY (SELECT 1 AS a) f", "tsql"),
])
def test_dialect_features_over_active_tables_still_run(sql, dialect):
    check_table_scope(sql, dialect, SCOPE)


APPLY_SCOPE = TableScope.from_names(active=["events"], inactive=["secret"])


@pytest.mark.parametrize("dialect", ["tsql", "fabric", "oracle"])
@pytest.mark.parametrize("sql", [
    "SELECT * FROM events CROSS APPLY secret",
    "SELECT * FROM events OUTER APPLY secret",
    "SELECT * FROM events CROSS APPLY dbo.fn_rows(1)",
    "SELECT * FROM events e CROSS APPLY fn_rows(e.id)",
])
def test_apply_over_a_name_or_a_call_is_refused(dialect, sql):
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, dialect, APPLY_SCOPE)


@pytest.mark.parametrize("dialect", ["tsql", "fabric", "oracle"])
@pytest.mark.parametrize("sql", [
    "SELECT * FROM events e CROSS APPLY (SELECT id FROM events f WHERE f.id = e.id) x",
    "SELECT * FROM events e OUTER APPLY (SELECT id FROM events f WHERE f.id = e.id) x",
])
def test_apply_over_a_subquery_of_enabled_tables_still_runs(dialect, sql):
    check_table_scope(sql, dialect, APPLY_SCOPE)


@pytest.mark.parametrize("dialect", ["tsql", "fabric", "oracle"])
def test_apply_over_a_subquery_of_a_disabled_table_is_refused(dialect):
    with pytest.raises(TableScopeViolation, match="SECRET is not enabled"):
        check_table_scope("SELECT * FROM events e CROSS APPLY (SELECT id FROM secret s WHERE s.id = e.id) x",
                          dialect, APPLY_SCOPE)


@pytest.mark.parametrize("dialect, sql", [
    ("postgres", "SELECT * FROM events e, LATERAL secret"),
    ("postgres", "SELECT * FROM events e, LATERAL other.fn_rows(e.id) r"),
    ("postgres", "SELECT * FROM events e CROSS JOIN LATERAL fn_rows(e.id) r"),
    ("snowflake", "SELECT * FROM events e, LATERAL secret"),
])
def test_lateral_over_a_name_or_an_unknown_call_is_refused(dialect, sql):
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, dialect, APPLY_SCOPE)


@pytest.mark.parametrize("dialect, sql", [
    ("postgres", "SELECT * FROM events e CROSS JOIN LATERAL jsonb_array_elements(e.items) i"),
    ("postgres", "SELECT * FROM events e, LATERAL generate_series(1, e.n) g"),
    ("postgres", "SELECT * FROM events e, LATERAL (SELECT 1 AS one) g"),
    ("databricks", "SELECT id, tag FROM events LATERAL VIEW explode(tags) t AS tag"),
    ("snowflake", "SELECT f.value FROM events e, LATERAL FLATTEN(input => e.items) f"),
])
def test_lateral_over_a_subquery_or_a_row_producing_function_still_runs(dialect, sql):
    check_table_scope(sql, dialect, APPLY_SCOPE)


# Row-producing functions of Spark, Databricks and Hive, and T-SQL's nodes()
# method of an xml column: each reads only its arguments.
@pytest.mark.parametrize("dialect, sql", [
    *[(d, s) for d in ("spark", "databricks", "hive") for s in (
        "SELECT id, a, b FROM events LATERAL VIEW json_tuple(payload, 'a', 'b') t AS a, b",
        "SELECT id, k, v FROM events LATERAL VIEW stack(2, 'a', a, 'b', b) t AS k, v",
        "SELECT id, h FROM events LATERAL VIEW parse_url_tuple(url, 'HOST') t AS h",
        "SELECT id, p, tag FROM events LATERAL VIEW posexplode(tags) t AS p, tag",
        "SELECT id, a FROM events LATERAL VIEW inline(items) t AS a",
        "SELECT id, a FROM events LATERAL VIEW inline_outer(items) t AS a",
        "SELECT id, tag FROM events LATERAL VIEW explode_outer(tags) t AS tag",
        "SELECT id, tag FROM events LATERAL VIEW OUTER explode(tags) t AS tag",
    )],
    ("databricks", "SELECT e.id, v.value FROM events e, LATERAL variant_explode(e.payload) v"),
    ("databricks", "SELECT e.id, v.value FROM events e, LATERAL variant_explode_outer(e.payload) v"),
    *[(d, s) for d in ("tsql", "fabric") for s in (
        "SELECT e.id, n.x.value('.', 'int') AS v FROM events e CROSS APPLY e.doc.nodes('/a/b') AS n(x)",
        "SELECT e.id FROM events e OUTER APPLY e.doc.nodes('/a/b') AS n(x)",
        "SELECT e.id FROM events e CROSS APPLY e.doc.nodes('/a') AS n(x) CROSS APPLY n.x.nodes('/b') AS m(y)",
        "SELECT events.id FROM events CROSS APPLY events.doc.nodes('/a/b') AS n(x)",
    )],
])
def test_a_function_that_makes_rows_from_a_column_still_runs(dialect, sql):
    check_table_scope(sql, dialect, APPLY_SCOPE)


@pytest.mark.parametrize("dialect, sql", [
    *[(d, s) for d in ("tsql", "fabric") for s in (
        "SELECT e.id FROM events e CROSS APPLY dbo.fn_rows(1)",
        "SELECT e.id FROM events e CROSS APPLY secret",
        "SELECT e.id FROM events e CROSS APPLY secret.nodes('/a') AS n(x)",
        "SELECT e.id FROM events e CROSS APPLY dbo.nodes('/a') AS n(x)",
        "SELECT e.id FROM events e CROSS APPLY dbo.secret.nodes('/a') AS n(x)",
        "SELECT e.id FROM events e CROSS APPLY otherdb.dbo.secret.nodes('/a') AS n(x)",
        "SELECT e.id FROM events e CROSS APPLY doc.nodes('/a') AS n(x)",
        "SELECT e.id FROM events e CROSS APPLY e.doc.query('/a') AS n(x)",
        "SELECT e.id FROM events e CROSS APPLY e.doc.nodes(e.id) AS n(x)",
        "SELECT e.id FROM events e CROSS APPLY e.doc.nodes('/a', 1) AS n(x)",
        "SELECT e.id FROM events e CROSS APPLY f.doc.nodes('/a') AS n(x)",
        "SELECT e.id FROM events e CROSS APPLY events.doc.nodes('/a') AS n(x)",
    )],
    ("oracle", "SELECT e.id FROM events e CROSS APPLY e.doc.nodes('/a') n"),
    *[(d, s) for d in ("spark", "databricks", "hive") for s in (
        "SELECT id, tag FROM events LATERAL VIEW my_udtf(tags) t AS tag",
        "SELECT id, tag FROM events LATERAL VIEW secret t AS tag",
    )],
    ("databricks", "SELECT e.id FROM events e, LATERAL read_files('/x') v"),
])
def test_apply_or_lateral_over_another_call_stays_refused(dialect, sql):
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, dialect, APPLY_SCOPE)


# A row limit is a number or a bound parameter. A name there is one more
# source to an engine that reads the clause word as the table's alias.
LIMIT_DIALECTS = ["tsql", "fabric", "oracle", "teradata", "trino", "presto", "athena", "snowflake", "spark",
                  "databricks", "postgres", "mysql", "bigquery", "clickhouse", "redshift", "sqlite", "druid"]


@pytest.mark.parametrize("dialect", LIMIT_DIALECTS)
@pytest.mark.parametrize("sql", [
    "SELECT * FROM events limit, secret",
    "SELECT * FROM events limit, ledger",
    "SELECT * FROM (SELECT * FROM events limit, ledger) t",
    "SELECT * FROM events e limit, sys.objects",
    "SELECT * FROM events LIMIT 5, ledger",
    "SELECT * FROM events OFFSET ledger",
    "SELECT * FROM events FETCH FIRST ledger ROWS ONLY",
])
def test_a_row_limit_that_is_a_name_is_refused(dialect, sql):
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, dialect, APPLY_SCOPE)


# The keyword as the alias of a joined table, with the join's USING or ON
# after it, and then one more source.
_AFTER_A_JOINED_ALIAS = tuple(
    "FROM events a JOIN events {k} " + condition + source
    for condition in ("USING (id)", "ON a.id = 1")
    for source in (", ledger", " JOIN ledger ON 1 = 1", " ledger"))


@pytest.mark.parametrize("dialect", sorted({d for d in CLIENT_DIALECTS.values() if d} | {"fabric", "presto"}))
def test_no_keyword_in_alias_position_leaves_a_source_unchecked(dialect):
    # After a table in FROM, with any keyword of the dialect where an alias
    # may stand, a name that follows as a source is checked as a table.
    # ClickHouse's ARRAY JOIN takes an array column, not a table.
    from sqlglot import Dialect

    keywords = sorted({k.lower() for k in Dialect.get_or_raise(dialect).tokenizer_class.KEYWORDS
                       if k.replace("_", "a").isalnum() and k[0].isalpha()})
    assert len(keywords) > 250 and {"limit", "offset", "fetch"} <= set(keywords)
    ran = []
    for keyword in keywords:
        for form in ("FROM events {k}, ledger", "FROM events {k} JOIN ledger ON 1 = 1",
                     "FROM events {k} CROSS JOIN ledger", "FROM (SELECT * FROM events {k}, ledger) t",
                     "FROM events AS {k}, ledger", *_AFTER_A_JOINED_ALIAS):
            try:
                check_table_scope("SELECT * " + form.format(k=keyword), dialect, APPLY_SCOPE)
            except TableScopeViolation:
                continue
            ran.append((keyword, form))
    assert ran == ([("array", "FROM events {k} JOIN ledger ON 1 = 1")] if dialect == "clickhouse" else [])


ALL_DIALECTS = sorted({d for d in CLIENT_DIALECTS.values() if d} | {"fabric", "presto"})


_FOR_XML = "SELECT * FROM events FOR XML AUTO, ledger"


# T-SQL, in which FOR is a reserved word, reads every name after FOR XML as
# one of its options, never as a source.
@pytest.mark.parametrize("dialect, sql", [(d, s) for d in ALL_DIALECTS for s in (
    "SELECT * FROM events FOR SYSTEM_TIME AS OF '2020-01-01', ledger",
    _FOR_XML,
    "SELECT * FROM events NEXT VALUE FOR seq, ledger",
    "SELECT * FROM events e FOR SYSTEM_TIME AS OF '2020-01-01' JOIN ledger ON 1 = 1",
    "SELECT * FROM sales..ledger",
    "SELECT * FROM events e, sales..ledger",
) if not (s == _FOR_XML and d in ("tsql", "fabric"))])
def test_a_source_after_a_for_clause_or_an_empty_name_part_is_checked(dialect, sql):
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, dialect, APPLY_SCOPE)


@pytest.mark.parametrize("dialect", ["tsql", "fabric"])
@pytest.mark.parametrize("sql", ["SELECT * FROM events FOR XML AUTO, ELEMENTS", _FOR_XML])
def test_the_options_of_for_xml_still_run_on_tsql(dialect, sql):
    check_table_scope(sql, dialect, APPLY_SCOPE)


# A WINDOW clause names each window as `name AS (specification)`. Anything
# else there is refused: an engine in which WINDOW is no keyword reads it as
# the alias of the table before it, and a name after it as one more source.
@pytest.mark.parametrize("dialect", ALL_DIALECTS)
@pytest.mark.parametrize("sql", [
    "SELECT * FROM events a JOIN events window USING (id), ledger",
    "SELECT * FROM events a JOIN events window USING (id), secret",
    "SELECT * FROM events a JOIN events window USING (id), sys.objects",
    "SELECT * FROM (SELECT * FROM events a JOIN events window USING (id), ledger) t",
    "SELECT * FROM events window x (id), ledger",
    "SELECT * FROM events WINDOW w AS (PARTITION BY id), ledger",
    "SELECT * FROM events WINDOW w AS ()",
    "SELECT * FROM events WINDOW w AS (PARTITION BY id), v AS ()",
    "SELECT * FROM events WINDOW w AS (PARTITION BY id), v AS (w)",
])
def test_a_window_clause_entry_without_a_specification_is_refused(dialect, sql):
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, dialect, APPLY_SCOPE)


# A window named by a keyword: what may follow a table's alias starts with
# one (USING, ON, WITH).
@pytest.mark.parametrize("dialect", ALL_DIALECTS)
@pytest.mark.parametrize("sql", [
    "SELECT * FROM events a JOIN events window USING (ORDER BY id)",
    "SELECT * FROM events window WITH (ORDER BY id)",
    "SELECT SUM(id) OVER w FROM events WINDOW w AS (ORDER BY id), using AS (PARTITION BY kind)",
])
def test_a_window_named_by_a_keyword_is_refused(dialect, sql):
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, dialect, APPLY_SCOPE)


@pytest.mark.parametrize("dialect", ["postgres", "trino", "redshift"])
def test_a_window_with_a_quoted_name_still_runs(dialect):
    check_table_scope('SELECT SUM(id) OVER "using" FROM events WINDOW "using" AS (ORDER BY id)', dialect, APPLY_SCOPE)


@pytest.mark.parametrize("dialect", ["postgres", "mysql", "bigquery", "spark", "databricks", "clickhouse", "trino",
                                     "sqlite", "duckdb", "redshift"])
@pytest.mark.parametrize("sql", [
    "SELECT SUM(id) OVER w FROM events WINDOW w AS (PARTITION BY kind ORDER BY id)",
    "SELECT SUM(id) OVER w, MAX(id) OVER v FROM events WINDOW w AS (PARTITION BY kind), v AS (ORDER BY id)",
    "SELECT SUM(id) OVER w FROM events WINDOW w AS (ORDER BY id ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)",
    "SELECT SUM(id) OVER w FROM events WINDOW v AS (PARTITION BY kind), w AS (v ORDER BY id)",
])
def test_a_window_clause_with_a_specification_still_runs(dialect, sql):
    check_table_scope(sql, dialect, APPLY_SCOPE)


# Spark, Databricks and Hive generators (json_tuple, stack, ...) make rows
# from their arguments under LATERAL VIEW and LATERAL there, by their bare
# name. In any other position, or on another dialect, the name is a function
# the checks do not know.
GENERATORS = ["inline_outer", "json_tuple", "parse_url_tuple", "stack", "variant_explode", "variant_explode_outer"]
GENERATOR_DIALECTS = ("databricks", "hive", "spark")
_UNDER_LATERAL = (
    "SELECT * FROM events e LATERAL VIEW {f}(e.id) t AS a",
    "SELECT * FROM events e LATERAL VIEW OUTER {f}(e.id) t AS a",
    "SELECT * FROM events e, LATERAL {f}(e.id) t",
    "SELECT * FROM events e CROSS JOIN LATERAL {f}(e.id) t",
)
_NOT_UNDER_LATERAL = (
    "SELECT * FROM {f}(1) t",
    "SELECT * FROM events e, {f}(e.id) t",
    "SELECT * FROM events e JOIN {f}(e.id) t ON 1 = 1",
    "SELECT * FROM events e CROSS JOIN {f}(e.id) t",
    "SELECT * FROM x.{f}(1) t",
    "SELECT * FROM events e, x.{f}(e.id) t",
    "SELECT * FROM events e LATERAL VIEW x.{f}(e.id) t AS a",
    "SELECT * FROM events e, LATERAL x.{f}(e.id) t",
    "SELECT * FROM events e CROSS JOIN LATERAL x.{f}(e.id) t",
    "SELECT * FROM TABLE({f}(1)) t",
    "SELECT * FROM events e, TABLE({f}(e.id)) t",
    "SELECT * FROM events e, LATERAL TABLE({f}(e.id)) t",
    "SELECT * FROM events e CROSS APPLY {f}(e.id) t",
    "SELECT * FROM events e OUTER APPLY {f}(e.id) t",
)


def _generator_forms_that_run(dialect, name, forms):
    ran = []
    for form in forms:
        try:
            check_table_scope(form.format(f=name), dialect, APPLY_SCOPE)
        except TableScopeViolation:
            continue
        ran.append(form)
    return ran


@pytest.mark.parametrize("dialect", ALL_DIALECTS)
@pytest.mark.parametrize("name", GENERATORS)
def test_a_generator_outside_lateral_is_refused_on_every_dialect(dialect, name):
    assert len(ALL_DIALECTS) == 20
    assert _generator_forms_that_run(dialect, name, _NOT_UNDER_LATERAL) == []


@pytest.mark.parametrize("dialect", ALL_DIALECTS)
@pytest.mark.parametrize("name", GENERATORS)
def test_a_generator_under_lateral_runs_only_on_spark_databricks_and_hive(dialect, name):
    expected = list(_UNDER_LATERAL) if dialect in GENERATOR_DIALECTS else []
    assert _generator_forms_that_run(dialect, name, _UNDER_LATERAL) == expected


@pytest.mark.parametrize("dialect", ["tsql", "fabric"])
def test_a_top_that_is_a_name_is_refused(dialect):
    with pytest.raises(TableScopeViolation, match="row limit"):
        check_table_scope("SELECT TOP (ledger) * FROM events", dialect, APPLY_SCOPE)


@pytest.mark.parametrize("dialect, sql", [
    *[(d, s) for d in LIMIT_DIALECTS for s in (
        "SELECT * FROM events LIMIT 10",
        "SELECT * FROM events LIMIT 10 OFFSET 5",
        "SELECT * FROM events ORDER BY id OFFSET 5 ROWS FETCH NEXT 10 ROWS ONLY",
        "SELECT * FROM events FETCH FIRST 5 ROWS ONLY",
    )],
    ("tsql", "SELECT TOP 5 * FROM events"),
    ("tsql", "SELECT TOP (5) PERCENT * FROM events"),
    ("tsql", "SELECT TOP (@n) * FROM events"),
    ("mysql", "SELECT * FROM events LIMIT 5, 10"),
    ("postgres", "SELECT * FROM events LIMIT $1 OFFSET $2"),
    ("bigquery", "SELECT * FROM events LIMIT @n"),
    ("clickhouse", "SELECT * FROM events LIMIT 2 BY id"),
    ("oracle", "SELECT * FROM events FETCH FIRST 5 PERCENT ROWS WITH TIES"),
])
def test_a_row_limit_that_is_a_number_or_a_parameter_still_runs(dialect, sql):
    check_table_scope(sql, dialect, APPLY_SCOPE)


POSTGRES_ROW_FUNCTIONS = [
    "json_to_record(o.j) AS r(a int)",
    "jsonb_to_record(o.j) AS r(a int)",
    "json_populate_record(null::sales.orders, o.j) r",
    "jsonb_populate_record(null::sales.orders, o.j) r",
    "jsonb_path_query(o.j, '$.items[*]') r",
    "json_object_keys(o.j) k",
    "jsonb_object_keys(o.j) k",
    "generate_subscripts(o.arr, 1) s",
    "json_to_recordset(o.j) AS r(a int)",
]


@pytest.mark.parametrize("join", ["{src}, LATERAL {fn}", "{src} LEFT JOIN LATERAL {fn} ON true", "{src}, {fn}"])
@pytest.mark.parametrize("fn", POSTGRES_ROW_FUNCTIONS)
def test_postgres_row_functions_over_an_active_table_still_run(fn, join):
    check_table_scope("SELECT * FROM " + join.format(src="sales.orders o", fn=fn), "postgres", SCOPE)


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


@pytest.mark.parametrize("dialect", DIALECTS)
def test_any_parser_exception_fails_closed(monkeypatch, dialect):
    from app.ai.code_execution import sql_parser

    def broken(sql, dialect):
        raise RecursionError("parser bug")  # not a sqlglot error

    monkeypatch.setattr(sql_parser, "parse_guard_sql", broken)
    with pytest.raises(TableScopeViolation, match="Could not verify"):
        check_table_scope("SELECT * FROM sales.orders", dialect, SCOPE)


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


# ---------------------------------------------------------------------------
# Clients whose SQL the check cannot parse (guard profile "nonparsing_sql")
# ---------------------------------------------------------------------------

NONPARSING_CLIENTS = [
    ("db2_client", "Db2Client"),
    ("ibmi_client", "IbmiClient"),
    ("vertica_client", "VerticaClient"),
    ("sybase_client", "SybaseClient"),
    ("sap_hana_client", "SapHanaClient"),
    ("pinot_client", "PinotClient"),
]


def _nonparsing_client(module, name, scope):
    """The real client class (so the guard knows it by type), with its
    transport replaced: every statement it is sent is recorded."""
    import importlib

    import pandas as pd

    parent = getattr(importlib.import_module(f"app.data_sources.clients.{module}"), name)
    sent = []

    def execute_query(self, sql):
        sent.append(sql)
        return pd.DataFrame({"n": [3]})

    client = object.__new__(type("Recording" + name, (parent,), {"execute_query": execute_query}))
    if scope is not None:
        setattr(client, TABLE_SCOPE_ATTR, scope)
    return client, sent


NONPARSING_CODE = (
    "def generate_df(ds_clients, excel_files):\n"
    "    return ds_clients['src'].execute_query('SELECT COUNT(*) AS n FROM orders')\n"
)


@pytest.mark.parametrize("module, name", NONPARSING_CLIENTS)
def test_a_client_without_a_parser_dialect_reads_enabled_tables_while_one_is_disabled(module, name):
    # The full matrix is in test_nonparsing_table_scope.py.
    client, sent = _nonparsing_client(module, name, TableScope.from_names(active=["orders"], inactive=["payroll"]))
    df, _, _ = StreamingCodeExecutor().execute_code(code=NONPARSING_CODE, ds_clients={"src": client}, excel_files=[])
    assert df["n"].iloc[0] == 3
    assert sent == ["SELECT COUNT(*) AS n FROM orders"]
    with pytest.raises(TableAccessError, match="PAYROLL is not enabled for this agent"):
        StreamingCodeExecutor().execute_code(code=NONPARSING_CODE.replace("orders", "payroll"),
                                             ds_clients={"src": client}, excel_files=[])
    assert sent == ["SELECT COUNT(*) AS n FROM orders"]


@pytest.mark.parametrize("module, name", NONPARSING_CLIENTS)
def test_a_client_the_check_cannot_parse_still_runs_when_no_table_is_disabled(module, name):
    client, sent = _nonparsing_client(module, name, TableScope.from_names(active=["orders", "customers"], inactive=[]))
    df, _, _ = StreamingCodeExecutor().execute_code(code=NONPARSING_CODE, ds_clients={"src": client}, excel_files=[])
    assert df["n"].iloc[0] == 3
    assert sent == ["SELECT COUNT(*) AS n FROM orders"]


def test_a_table_disabled_only_in_another_spelling_still_counts_as_disabled():
    # "Orders" enabled and "ORDERS" disabled fold to one name, but an engine
    # that compares quoted names exactly holds two tables.
    client, sent = _nonparsing_client("db2_client", "Db2Client",
                                      TableScope.from_names(active=["Orders"], inactive=["ORDERS"]))
    with pytest.raises(TableScopeViolation, match="cannot be checked for this connection type"):
        enforce_table_scope(client, "SELECT 1 FROM orders")


# ---------------------------------------------------------------------------
# CTE names only shadow tables where the CTE is visible
# ---------------------------------------------------------------------------

# A CTE is visible in the query that defines it and in queries nested in it,
# never in an outer or sibling query. Inside a CTE body only earlier CTEs are
# visible (plus itself under RECURSIVE).
CTE_SHADOWING_BYPASSES = [
    # Nested CTE named like the inactive table; the outer FROM is the real one.
    "SELECT * FROM payroll WHERE EXISTS (WITH payroll AS (SELECT 1) SELECT * FROM payroll)",
    "SELECT * FROM (WITH payroll AS (SELECT 1) SELECT * FROM payroll) a, payroll",
    # Sibling subquery: the CTE in one does not cover the other.
    "SELECT * FROM (WITH payroll AS (SELECT 1) SELECT * FROM payroll) a "
    "JOIN (SELECT * FROM payroll) b ON 1 = 1",
    # A non-recursive CTE's body cannot see itself.
    "WITH payroll AS (SELECT * FROM payroll) SELECT * FROM payroll",
    # A CTE body cannot see CTEs defined after it.
    "WITH b AS (SELECT * FROM payroll), payroll AS (SELECT 1) SELECT * FROM b",
]

CTE_LEGITIMATE = [
    "WITH payroll AS (SELECT * FROM sales.orders) SELECT * FROM payroll",
    "WITH x AS (SELECT * FROM sales.orders) SELECT * FROM (SELECT * FROM x) s",
    "WITH x AS (SELECT * FROM sales.orders), y AS (SELECT * FROM x) SELECT * FROM y",
    "WITH x AS (SELECT * FROM sales.orders) SELECT * FROM x UNION ALL SELECT * FROM x",
    "SELECT * FROM sales.orders WHERE id IN (WITH x AS (SELECT 1 AS id) SELECT id FROM x)",
]


@pytest.mark.parametrize("dialect", ["postgres", "snowflake", "sqlite", "tsql", "bigquery"])
@pytest.mark.parametrize("sql", CTE_SHADOWING_BYPASSES)
def test_cte_names_do_not_shadow_tables_outside_their_scope(dialect, sql):
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, dialect, SCOPE)


# The same selection without PAYROLL disabled: a CTE may not take a
# disabled table's name, so the CTE readings below are checked with the
# name unlisted (neither enabled nor disabled), which table scope still
# refuses when it is read as a table.
PAYROLL_UNLISTED = TableScope.from_names(active=["sales.orders", "sales.customers"], inactive=["hr.salaries"])


@pytest.mark.parametrize("dialect", ["postgres", "snowflake", "sqlite", "tsql", "bigquery"])
@pytest.mark.parametrize("sql", CTE_LEGITIMATE)
def test_ctes_in_scope_still_run(dialect, sql):
    check_table_scope(sql, dialect, PAYROLL_UNLISTED)
    if "payroll" not in sql:
        check_table_scope(sql, dialect, SCOPE)


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("sql", [
    "WITH payroll AS (SELECT * FROM sales.orders) SELECT * FROM payroll",
    "WITH Payroll AS (SELECT * FROM sales.orders) SELECT * FROM Payroll",
    "SELECT * FROM sales.orders WHERE id IN (WITH SALARIES AS (SELECT 1 AS id) SELECT id FROM SALARIES)",
])
def test_a_cte_named_after_a_disabled_table_is_refused(dialect, sql):
    with pytest.raises(TableScopeViolation, match="a WITH entry may not take its name"):
        check_table_scope(sql, dialect, SCOPE)


# A table reference is a CTE only when it is written exactly as the CTE's
# name: same letters, same case, same quoting. Engines differ in how they
# compare the two (ClickHouse and MySQL 8 compare CTE names case by case;
# Snowflake and Oracle fold unquoted names to upper case and keep quoted
# ones), so any other spelling is checked as the table it may be.
CTE_NAMED_OTHERWISE = [
    "WITH PAYROLL AS (SELECT 1) SELECT * FROM payroll",
    "WITH Payroll AS (SELECT 1) SELECT * FROM sales.orders JOIN payroll ON 1 = 1",
    "WITH payroll AS (SELECT 1) SELECT * FROM (SELECT * FROM PAYROLL) t",
]


@pytest.mark.parametrize("dialect", ["mysql", "clickhouse", "snowflake", "oracle", "tsql", "bigquery", "duckdb",
                                     "trino", "databricks", "sqlite"])
@pytest.mark.parametrize("sql", CTE_NAMED_OTHERWISE)
def test_a_cte_written_in_another_case_does_not_stand_for_a_table(dialect, sql):
    with pytest.raises(TableScopeViolation, match="PAYROLL"):
        check_table_scope(sql, dialect, SCOPE)


@pytest.mark.parametrize("dialect", ["snowflake", "oracle", "redshift"])
@pytest.mark.parametrize("sql", [
    'WITH payroll AS (SELECT 1) SELECT * FROM "payroll"',
    'WITH "PAYROLL" AS (SELECT 1) SELECT * FROM PAYROLL',
])
def test_a_cte_quoted_otherwise_does_not_stand_for_a_table(dialect, sql):
    with pytest.raises(TableScopeViolation, match="PAYROLL"):
        check_table_scope(sql, dialect, SCOPE)


@pytest.mark.parametrize("sql", [
    "WITH PAYROLL AS (SELECT 1) SELECT * FROM payroll",
    'WITH payroll AS (SELECT 1) SELECT * FROM "payroll"',
])
def test_postgres_compares_a_cte_name_as_postgres_folds_it(sql):
    # Postgres folds unquoted names (ASCII letters) to lower case and keeps
    # quoted ones: these name the CTE, as table scope compares Postgres
    # names; a quoted name in another case does not.
    check_table_scope(sql, "postgres", PAYROLL_UNLISTED)
    with pytest.raises(TableScopeViolation, match="PAYROLL"):
        check_table_scope('WITH "PAYROLL" AS (SELECT 1) SELECT * FROM payroll', "postgres", PAYROLL_UNLISTED)


@pytest.mark.parametrize("dialect", ["mysql", "clickhouse", "snowflake", "oracle", "tsql", "postgres"])
def test_a_cte_written_as_its_name_still_runs(dialect):
    check_table_scope("WITH Payroll AS (SELECT * FROM sales.orders) SELECT * FROM Payroll", dialect,
                      PAYROLL_UNLISTED)


def test_tables_read_lists_the_tables_a_statement_reads_by_name_without_its_cte_references():
    """The one walk table scope and the lexer oracle share: every table
    named as an identifier, in order, once each; a CTE reference visible
    where it stands is not a table."""
    from app.ai.code_execution import sql_parser, table_scope

    sql = ("WITH x AS (SELECT * FROM x), y AS (SELECT * FROM sales.orders) "
           "SELECT * FROM x JOIN y ON true JOIN Warehouse.Sales.Payroll p ON true, generate_series(1, 2) g "
           "WHERE EXISTS (SELECT 1 FROM sales.orders)")
    [statement] = sql_parser.parse_guard_sql(sql, "postgres")
    found = table_scope.tables_read(statement)
    assert len(found) == 3
    assert set(found) == {("x",), ("sales", "orders"), ("warehouse", "sales", "payroll")}


def test_recursive_cte_can_reference_itself():
    check_table_scope(
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT * FROM r",
        "postgres", SCOPE,
    )


def test_a_duckdb_scope_without_its_duckdb_folded_names_is_unverified_and_refused():
    # A scope built by hand, not by from_names, has no names folded the way
    # DuckDB folds them: nothing is allowed rather than comparing otherwise.
    scope = TableScope(active=frozenset({("sales", "orders")}))
    with pytest.raises(TableScopeViolation) as refused:
        check_table_scope("SELECT * FROM sales.orders", "duckdb", scope)
    assert str(refused.value).startswith("Could not verify which tables this query reads")


def test_a_scope_from_names_carries_duckdb_folded_names():
    scope = TableScope.from_names(active=["Sales.Orders"], inactive=[])
    assert scope.ascii_folded is not None and scope.ascii_folded.active == frozenset({("sales", "orders")})
    check_table_scope("SELECT * FROM SALES.ORDERS", "duckdb", scope)


NESTED_CTE_SHADOW_CODE = (
    "def generate_df(ds_clients, excel_files):\n"
    "    return ds_clients['clinic'].execute_query(\n"
    "        'SELECT name FROM patients WHERE EXISTS '\n"
    "        '(WITH patients AS (SELECT 1) SELECT * FROM patients)')\n"
)


def test_sandbox_code_cannot_hide_an_inactive_table_behind_a_nested_cte(clinic_db):
    with pytest.raises(TableAccessError):
        StreamingCodeExecutor().execute_code(
            code=NESTED_CTE_SHADOW_CODE, ds_clients={"clinic": _scoped_client(clinic_db)}, excel_files=[]
        )


# ---------------------------------------------------------------------------
# Athena: databases named in the table, and no default database
# ---------------------------------------------------------------------------

ATHENA_SCOPE = TableScope.from_names(
    active=["globex_db.assets", "acme_db.assets", "globex_db.my-table"],
    inactive=["other_db.secrets"],
)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM globex_db.assets",
    "SELECT * FROM awsdatacatalog.globex_db.assets",
    'SELECT * FROM "globex_db"."my-table"',
])
def test_athena_allows_active_database_qualified_tables(sql):
    check_table_scope(sql, "athena", ATHENA_SCOPE)


def test_athena_refuses_a_table_of_an_inactive_database():
    scope = TableScope.from_names(active=["globex_db.assets"], inactive=["acme_db.assets"])
    with pytest.raises(TableScopeViolation):
        check_table_scope("SELECT * FROM acme_db.assets", "athena", scope)


def test_unqualified_name_passes_is_allowed_when_every_namesake_is_active_but_is_found_by_one_part_check():
    scope = TableScope.from_names(active=["globex_db.assets", "acme_db.assets"], inactive=[])
    assert is_allowed(("assets",), scope) is True
    assert one_part_table_refs("SELECT * FROM assets", "athena") == ["assets"]


def test_one_part_check_ignores_qualified_names_and_ctes():
    assert one_part_table_refs("SELECT * FROM globex_db.assets a JOIN acme_db.assets b ON a.id = b.id", "athena") == []
    assert one_part_table_refs("WITH users AS (SELECT 1 AS id) SELECT * FROM users", "athena") == []
    assert one_part_table_refs("SELECT 1", "athena") == []


@pytest.mark.parametrize("sql", [
    "SELECT FROM FROM (",
    "SHOW TABLES IN globex_db",
    "DESCRIBE globex_db.assets",
    "USE globex_db",
])
def test_one_part_check_refuses_what_it_cannot_verify(sql):
    with pytest.raises(TableScopeViolation):
        one_part_table_refs(sql, "athena")


EXACT_SCOPE = TableScope.from_names(active=["globex_db.asset", "asset"], inactive=["acme_db.asset"])


@pytest.mark.parametrize("sql", [
    "SELECT * FROM asset",
    "SELECT * FROM acme_db.asset",
    "SELECT * FROM billing_db.asset",
    "SELECT * FROM othercat.globex_db.asset",
    "SELECT * FROM a.b.globex_db.asset",
    # A WITH entry may not take a disabled table's name (acme_db.asset), with
    # or without exact catalogs.
    "WITH asset AS (SELECT 1 AS id) SELECT * FROM asset",
])
def test_exact_scope_refuses_what_suffix_matching_would_let_through(sql):
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, "athena", EXACT_SCOPE, exact_catalogs=("awsdatacatalog",))


@pytest.mark.parametrize("sql", [
    "SELECT * FROM globex_db.asset",
    "SELECT * FROM awsdatacatalog.globex_db.asset",
    "WITH recent AS (SELECT 1 AS id) SELECT * FROM recent",
])
def test_exact_scope_allows_database_qualified_active_tables_and_ctes(sql):
    check_table_scope(sql, "athena", EXACT_SCOPE, exact_catalogs=("awsdatacatalog",))


def test_default_scope_matching_is_unchanged_by_the_exact_option():
    scope = TableScope.from_names(active=["globex_db.asset", "asset"], inactive=[])
    check_table_scope("SELECT * FROM billing_db.asset", "athena", scope)


UNIFIED_SCOPE = TableScope.from_names(active=["public.asset"], inactive=[])

UNIFIED_REFUSED = [
    "SELECT * FROM postgres_query('__t0001', 'select 1')",
    "SELECT * FROM postgres_execute('__t0001', 'drop table x')",
    "SELECT * FROM read_csv('f.csv')",
    "SELECT * FROM 'f.csv'",
    "SELECT * FROM __t0001.public.asset",
    "SELECT postgres_query('__t0001', 'select 1')",
    "SELECT postgres_execute('__t0001', 'drop table x')",
    "SELECT postgres_attach('x')",
]

UNIFIED_ALLOWED = [
    "SELECT tenant_db, count(*) FROM public.asset GROUP BY 1",
    "SELECT * FROM memory.public.asset",
]


@pytest.mark.parametrize("sql", UNIFIED_REFUSED)
def test_duckdb_unified_scope_refuses_escape_hatches(sql):
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, "duckdb", UNIFIED_SCOPE, ("memory",))


@pytest.mark.parametrize("sql", UNIFIED_ALLOWED)
def test_duckdb_unified_scope_allows_unified_views(sql):
    check_table_scope(sql, "duckdb", UNIFIED_SCOPE, ("memory",))
