"""Ordinary queries keep passing every check.

Contract: every query in `LEGIT_QUERIES` (the shapes agents, saved steps and
custom tables send: date parts, casts, quoted names, comments, dollar
quotes, backslashes in literals, CTEs, window functions) passes the
read-only guard for its dialect, and table scope accepts the text the guard
returned when every table it reads is active. A query scoped to the
viewer's identity, rendered by the connection's own renderer (values with a
quote or a backslash included), passes both checks and still counts as
filtered on that identity.

A failure here is an over-refusal: a legitimate query the checks no longer
let through. Over-refusals known today are listed with their reason in
`KNOWN_OVER_REFUSALS` and expected to fail until their rule is fixed.
"""

from __future__ import annotations

import importlib

import pytest

from app.ai.code_execution.client_profiles import family_name
from app.ai.code_execution.identity_bind import CapturedQuery
from app.ai.code_execution.query_params import render_sql_with_params, renderer_for, verify_identity_binds_in_queries
from app.ai.code_execution.sql_guard import ensure_read_only
from app.ai.code_execution.table_scope import TableScope, check_table_scope
from app.schemas.param_schema import ParamSpec
from tests.unit.sql_corpus.legit_queries import CORPUS_TABLES, IDENTITY_QUERIES, KNOWN_OVER_REFUSALS, LEGIT_QUERIES

BS = chr(92)

# The connector each dialect's queries are sent to.
CLIENTS = {
    "postgres": ("postgresql_client", "PostgresqlClient"),
    "redshift": ("aws_redshift_client", "AwsRedshiftClient"),
    "mysql": ("mysql_client", "MysqlClient"),
    "tsql": ("mssql_client", "MSSQLClient"),
    "oracle": ("oracledb_client", "OracledbClient"),
    "sqlite": ("sqlite_client", "SqliteClient"),
    "snowflake": ("snowflake_client", "SnowflakeClient"),
    "bigquery": ("bigquery_client", "BigqueryClient"),
    "athena": ("aws_athena_client", "AwsAthenaClient"),
    "trino": ("trino_client", "TrinoClient"),
    "clickhouse": ("clickhouse_client", "ClickhouseClient"),
    "databricks": ("databricks_sql_client", "DatabricksSqlClient"),
    "duckdb": ("duckdb_client", "DuckDBClient"),
}

SCOPE = TableScope.from_names(active=CORPUS_TABLES, inactive=[])
SPEC = [ParamSpec.model_validate({"name": "owner", "type": "string", "source": "identity"})]


def _client(dialect: str):
    module, name = CLIENTS[dialect]
    cls = getattr(importlib.import_module(f"app.data_sources.clients.{module}"), name)
    # No connection is opened: the checks read the client's type only.
    return object.__new__(cls)


def _ids(entries) -> list[str]:
    return [f"{entry[0]}-{i}" for i, entry in enumerate(entries)]


def test_the_corpus_covers_every_dialect_and_the_shapes_that_must_keep_working():
    dialects = {dialect for dialect, _sql in LEGIT_QUERIES}
    assert dialects == set(CLIENTS)
    postgres = [sql for dialect, sql in LEGIT_QUERIES if dialect == "postgres"]
    assert any("date_part('year'" in sql for sql in postgres)
    assert any(BS in sql for sql in postgres), "a backslash in a standard literal"
    assert any("E'" in sql for sql in postgres) and any("$$" in sql for sql in postgres)
    assert any("--" in sql for sql in postgres) and any("/*" in sql for sql in postgres)
    assert any('"' in sql for sql in postgres), "a quoted name"
    assert len(postgres) >= 40
    values = {value for _dialect, _template, value in IDENTITY_QUERIES}
    assert "DOMAIN" + BS + "user" in values and any("'" in value for value in values)
    assert any("date_part('year'" in template for _d, template, _v in IDENTITY_QUERIES)
    assert {dialect for dialect, _t, _v in IDENTITY_QUERIES} >= {"postgres", "mysql", "tsql", "snowflake"}


@pytest.mark.parametrize("dialect,sql", LEGIT_QUERIES, ids=_ids(LEGIT_QUERIES))
def test_a_legitimate_query_passes_the_guard_and_table_scope(dialect, sql):
    checked = ensure_read_only(_client(dialect), sql)
    check_table_scope(checked, dialect, SCOPE)


@pytest.mark.parametrize("dialect,sql", [
    pytest.param(dialect, sql, marks=pytest.mark.xfail(strict=True, reason=f"known over-refusal: {reason}"))
    for dialect, sql, reason in KNOWN_OVER_REFUSALS
], ids=_ids(KNOWN_OVER_REFUSALS))
def test_a_known_over_refusal_is_still_refused_until_its_rule_is_fixed(dialect, sql):
    """Strict: once the checks let one of these through, this fails, and
    the query moves to LEGIT_QUERIES."""
    test_a_legitimate_query_passes_the_guard_and_table_scope(dialect, sql)


@pytest.mark.parametrize("dialect,template,value", IDENTITY_QUERIES, ids=_ids(IDENTITY_QUERIES))
def test_an_identity_scoped_query_passes_every_check_and_binds_the_identity(dialect, template, value):
    client = _client(dialect)
    rendered = render_sql_with_params(template, {"owner": value}, renderer=renderer_for(client))
    checked = ensure_read_only(client, rendered)
    check_table_scope(checked, dialect, SCOPE)
    capture = CapturedQuery(checked, family_name(client))
    assert verify_identity_binds_in_queries([capture], {"owner": value}, SPEC) is None
