"""The preview query names its table with a dialect-correct quoted reference.

The catalog stores names as ``<namespace>.<table>`` (namespace recorded in
``metadata_json``) or bare; the reference must address exactly that relation
whatever characters the source allowed in it.
"""
from types import SimpleNamespace

import pytest

from app.models.connection_table import KIND_BOW, KIND_TABLE
from app.services.table_preview_service import (
    is_previewable_table,
    is_previewable_type,
    preview_sql,
    quoted_table_ref,
)


def _table(name, meta=None, kind=KIND_TABLE):
    return SimpleNamespace(name=name, metadata_json=meta, kind=kind)


@pytest.mark.parametrize(
    "conn_type, table, expected",
    [
        ("postgresql", _table("public.orders", {"schema": "public"}), '"public"."orders"'),
        ("snowflake", _table("SALES.ORDERS", {"schema": "SALES"}), '"SALES"."ORDERS"'),
        ("oracledb", _table("HR.EMPLOYEES", {"schema": "HR"}), '"HR"."EMPLOYEES"'),
        ("mssql", _table("dbo.Order Lines", {"schema": "dbo"}), "[dbo].[Order Lines]"),
        ("MSSQL", _table("dbo.t", {"schema": "dbo"}), "[dbo].[t]"),
        ("clickhouse", _table("analytics.events", {"database": "analytics"}), "`analytics`.`events`"),
        ("bigquery", _table("ds.tbl", {"dataset": "ds"}), "`ds`.`tbl`"),
        ("mysql", _table("orders"), "`orders`"),
        ("sqlite", _table("order", {"database": "/data/x.sqlite"}), '"order"'),
    ],
)
def test_reference_is_quoted_per_dialect(conn_type, table, expected):
    assert quoted_table_ref(conn_type, table) == expected


@pytest.mark.parametrize(
    "conn_type, name, expected",
    [
        ("postgresql", 'we"ird', '"we""ird"'),
        ("mysql", "we`ird", "`we``ird`"),
        ("mssql", "we]ird", "[we]]ird]"),
    ],
)
def test_closing_quote_inside_a_name_is_escaped(conn_type, name, expected):
    assert quoted_table_ref(conn_type, _table(name)) == expected


def test_dot_inside_the_table_name_stays_in_the_table_part():
    # Split on the recorded namespace, not on the first dot.
    t = _table("public.v1.2_snapshot", {"schema": "public"})
    assert quoted_table_ref("postgresql", t) == '"public"."v1.2_snapshot"'


def test_only_select_able_sql_relations_are_previewable():
    assert is_previewable_type("postgresql")
    for not_sql in ("powerbi", "posthog", "csv", "mcp", None, ""):
        assert not is_previewable_type(not_sql)
    # A custom table is served from its artifact, not the source.
    assert not is_previewable_table("postgresql", _table("x", kind=KIND_BOW))
    # Snowflake semantic views are not SELECT * targets.
    sv = _table("S.V", {"schema": "S", "type": "semantic_view"})
    assert not is_previewable_table("snowflake", sv)
    assert is_previewable_table("snowflake", _table("S.T", {"schema": "S"}))


@pytest.mark.parametrize(
    "conn_type, table, expected",
    [
        ("mssql", _table("dbo.Orders", {"schema": "dbo"}), "SELECT TOP 50 * FROM [dbo].[Orders]"),
        ("ms_fabric", _table("dbo.t", {"schema": "dbo"}), "SELECT TOP 50 * FROM [dbo].[t]"),
        ("sybase", _table("dbo.t", {"schema": "dbo"}), "SELECT TOP 50 * FROM [dbo].[t]"),
        # ROWNUM, not FETCH FIRST: Oracle 11g has no FETCH FIRST.
        ("oracledb", _table("HR.EMP", {"schema": "HR"}), 'SELECT * FROM "HR"."EMP" WHERE ROWNUM <= 50'),
        ("postgresql", _table("public.o", {"schema": "public"}), 'SELECT * FROM "public"."o" LIMIT 50'),
        ("mysql", _table("orders"), "SELECT * FROM `orders` LIMIT 50"),
        ("bigquery", _table("ds.t", {"dataset": "ds"}), "SELECT * FROM `ds`.`t` LIMIT 50"),
        ("snowflake", _table("S.T", {"schema": "S"}), 'SELECT * FROM "S"."T" LIMIT 50'),
        ("clickhouse", _table("db.e", {"database": "db"}), "SELECT * FROM `db`.`e` LIMIT 50"),
    ],
)
def test_default_query_writes_the_row_bound_in_the_dialects_syntax(conn_type, table, expected):
    assert preview_sql(conn_type, table, 50) == expected
