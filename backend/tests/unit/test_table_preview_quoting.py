"""The preview query names its table with a dialect-correct quoted reference.

The catalog stores names as ``<namespace>.<table>`` (namespace recorded in
``metadata_json``) or bare; the reference must address exactly that relation
whatever characters the source allowed in it.
"""
from types import SimpleNamespace

import pytest

from app.models.connection_table import KIND_BOW, KIND_TABLE
from app.services.custom_query_service import ACCELERABLE_TYPES
from app.services.table_preview_service import (
    MAX_NAMED_COLUMNS,
    base_table_sql,
    is_previewable_table,
    is_previewable_type,
    preview_sql,
    quoted_table_ref,
)


def _table(name, meta=None, kind=KIND_TABLE, columns=None, deleted_at=None):
    return SimpleNamespace(
        name=name, metadata_json=meta, kind=kind, columns=columns or [], deleted_at=deleted_at,
    )


def _cols(*names):
    return [{"name": n, "dtype": "text"} for n in names]


@pytest.mark.parametrize(
    "conn_type, table, expected",
    [
        ("postgresql", _table("public.orders", {"schema": "public"}), '"public"."orders"'),
        ("snowflake", _table("SALES.ORDERS", {"schema": "SALES"}), '"SALES"."ORDERS"'),
        # One database: SCHEMA.TABLE even though the database is recorded.
        ("snowflake", _table("SALES.ORDERS", {"schema": "SALES", "database": "DB_A"}), '"SALES"."ORDERS"'),
        # Several databases on one connection: DATABASE.SCHEMA.TABLE.
        ("snowflake", _table("DB_B.CRM.CUSTOMERS", {"schema": "CRM", "database": "DB_B"}), '"DB_B"."CRM"."CUSTOMERS"'),
        ("ms_fabric", _table("dbo.orders", {"schema": "dbo", "database": "lake"}), "[dbo].[orders]"),
        ("oracledb", _table("HR.EMPLOYEES", {"schema": "HR"}), '"HR"."EMPLOYEES"'),
        ("mssql", _table("dbo.Order Lines", {"schema": "dbo"}), "[dbo].[Order Lines]"),
        ("MSSQL", _table("dbo.t", {"schema": "dbo"}), "[dbo].[t]"),
        ("clickhouse", _table("analytics.events", {"database": "analytics"}), "`analytics`.`events`"),
        ("bigquery", _table("ds.tbl", {"dataset": "ds"}), "`ds`.`tbl`"),
        ("mysql", _table("orders"), "`orders`"),
        ("sqlite", _table("order", {"database": "/data/x.sqlite"}), "`order`"),
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
    for no_relations in ("csv", "mcp", "google_drive", None, ""):
        assert not is_previewable_type(no_relations)
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


def test_every_cacheable_type_can_be_previewed():
    # The preview is the on-ramp to a custom table, so a type that can be
    # cached must never silently lose its preview when added to one list only.
    missing = {t for t in ACCELERABLE_TYPES if not is_previewable_type(t)}
    assert not missing


@pytest.mark.parametrize(
    "conn_type, table, expected",
    [
        # Sybase stores "owner.table" with no namespace in metadata.
        ("sybase", _table("dba.orders"), "[dba].[orders]"),
        # Three-part names split on the recorded catalog + schema.
        ("trino", _table("hive.web.clicks", {"catalog": "hive", "schema": "web"}), '"hive"."web"."clicks"'),
        ("databricks_sql", _table("main.sales.o", {"catalog": "main", "schema": "sales"}), "`main`.`sales`.`o`"),
        # Two-part name where the client also recorded a catalog it left out of the name.
        ("spark_connect", _table("db.t", {"schema": "db", "catalog": "spark_catalog"}), "`db`.`t`"),
        # Bare name, schema only in metadata: qualified, not left to the search path.
        ("aws_redshift", _table("events", {"schema": "analytics"}), '"analytics"."events"'),
        ("teradata", _table("DBC.T", {"schema": "DBC"}), '"DBC"."T"'),
    ],
)
def test_reference_resolves_each_clients_naming(conn_type, table, expected):
    assert quoted_table_ref(conn_type, table) == expected


def test_catalog_columns_are_named_instead_of_star():
    t = _table("public.o", {"schema": "public"}, columns=_cols("id", 'we"ird'))
    sql = preview_sql("postgresql", t, 10)
    assert sql == 'SELECT "id", "we""ird" FROM "public"."o" LIMIT 10'
    assert base_table_sql("postgresql", t) == 'SELECT "id", "we""ird" FROM "public"."o"'
    assert preview_sql("mssql", _table("dbo.t", {"schema": "dbo"}, columns=_cols("a")), 5) == "SELECT TOP 5 [a] FROM [dbo].[t]"


def test_very_wide_tables_fall_back_to_star():
    wide = _cols(*[f"c{i}" for i in range(MAX_NAMED_COLUMNS + 1)])
    assert preview_sql("postgresql", _table("t", columns=wide), 10) == 'SELECT * FROM "t" LIMIT 10'


def test_power_bi_samples_with_dax_against_its_own_model():
    meta = {"powerbi": {"datasetId": "d1", "tableName": "Sales Rep's"}}
    t = _table("Model/Sales Rep's", meta, columns=_cols("x"))
    assert is_previewable_table("powerbi", t)
    assert preview_sql("powerbi", t, 50) == "EVALUATE 'Sales Rep''s'"
    # Without its semantic model there is nothing to run the DAX against.
    assert not is_previewable_table("powerbi", _table("Model/T", {"powerbi": {}}))


def test_posthog_samples_with_bounded_hogql():
    assert preview_sql("posthog", _table("events", columns=_cols("event")), 50) == "SELECT * FROM `events` LIMIT 50"


def test_removed_catalog_rows_are_not_previewable():
    gone = _table("public.o", {"schema": "public"}, deleted_at="2026-01-01")
    assert not is_previewable_table("postgresql", gone)


@pytest.mark.parametrize(
    "conn_type, name, expected",
    [
        # Bare when typing it bare means the same name.
        ("postgresql", "orders", "orders"),
        ("snowflake", "ORDERS", "ORDERS"),
        ("mysql", "OrderLines", "OrderLines"),
        # Quoted when the dialect would fold its case...
        ("postgresql", "TrackId", '"TrackId"'),
        ("snowflake", "orders", '"orders"'),
        ("oracledb", "Emp", '"Emp"'),
        # ...read it as a keyword, or stop at an odd character.
        ("mysql", "order", "`order`"),
        ("mssql", "user", "[user]"),
        ("postgresql", "line items", '"line items"'),
        ("bigquery", "2024_sales", "`2024_sales`"),
    ],
)
def test_suggestions_are_quoted_only_when_the_dialect_needs_it(conn_type, name, expected):
    from app.services.table_preview_service import insert_ident, spec_for

    assert insert_ident(spec_for(conn_type), name) == expected


def test_completion_catalog_inserts_qualified_table_names_and_skips_dax():
    from app.services.table_preview_service import completion_catalog

    t = _table("Sales.Order Lines", {"schema": "Sales"}, columns=_cols("Id", "qty"))
    cat = completion_catalog("postgresql", [t, _table("x", kind=KIND_BOW)])
    assert cat["language"] == "sql"
    assert [x["name"] for x in cat["tables"]] == ["Sales.Order Lines"]
    assert cat["tables"][0]["insert"] == '"Sales"."Order Lines"'
    assert [c["insert"] for c in cat["tables"][0]["columns"]] == ['"Id"', "qty"]

    pbi = _table("M/T", {"powerbi": {"datasetId": "d"}}, columns=_cols("a"))
    assert completion_catalog("powerbi", [pbi])["tables"] == []
