"""Redshift schema discovery exposes every queryable relation, not just base tables.

Customers query Redshift through views (often dbt's late-binding views,
``CREATE VIEW ... WITH NO SCHEMA BINDING``). Redshift lists ordinary views in
information_schema, but late-binding view columns only come from
``pg_get_late_binding_view_cols()``. The driver is mocked at the psycopg2
boundary by a tiny catalog that answers the two sources the way Redshift does.
"""
from contextlib import contextmanager

import pytest

from app.data_sources.clients.aws_redshift_client import AwsRedshiftClient

SCHEMA = "analytics"


class FakeRedshiftCatalog:
    """Answers information_schema and pg_get_late_binding_view_cols() queries."""

    def __init__(self, relations, late_binding_views, late_binding_error=None):
        # relations: {(schema, name): (table_type, [(column, dtype)])}
        self.relations = relations
        # late_binding_views: {(schema, name): [(column, col_type)]}
        self.late_binding_views = late_binding_views
        self.late_binding_error = late_binding_error

    def answer(self, sql, params):
        if "pg_get_late_binding_view_cols" in sql:
            if self.late_binding_error:
                raise self.late_binding_error
            (schema,) = params
            return [
                (name, col, col_type, pos)
                for (sch, name), cols in self.late_binding_views.items()
                if sch == schema
                for pos, (col, col_type) in enumerate(cols, start=1)
            ]
        if "information_schema.tables t" in sql:
            (schema,) = params
            enriched = "pg_description" in sql
            rows = []
            for (sch, name), (table_type, cols) in self.relations.items():
                if sch != schema or f"'{table_type}'" not in sql:
                    continue  # honour the query's table_type filter
                for pos, (col, dtype) in enumerate(cols, start=1):
                    row = (name, col, dtype, pos)
                    rows.append(row + (None, None) if enriched else row)
            return rows
        if "SELECT DISTINCT table_schema" in sql:
            return [(sch,) for sch in {s for s, _ in self.relations}]
        raise AssertionError(f"unexpected query: {sql}")


class FakeCursor:
    def __init__(self, catalog):
        self.catalog = catalog
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.rows = self.catalog.answer(sql, params)

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, catalog):
        self.catalog = catalog

    def cursor(self):
        return FakeCursor(self.catalog)

    def rollback(self):
        pass


def _client(monkeypatch, catalog):
    client = AwsRedshiftClient(
        host="example.redshift.amazonaws.com", port=5439, database="dev",
        schema=SCHEMA, user="u", password="p",
    )

    @contextmanager
    def connect():
        yield FakeConnection(catalog)

    monkeypatch.setattr(client, "connect", connect)
    return client


def _catalog(n_tables, n_views, n_late_binding, late_binding_error=None):
    relations = {}
    for i in range(n_tables):
        relations[(SCHEMA, f"table_{i}")] = ("BASE TABLE", [("id", "integer"), ("name", "character varying")])
    for i in range(n_views):
        relations[(SCHEMA, f"view_{i}")] = ("VIEW", [("day", "date"), ("total", "numeric")])
    # Same-named objects in another schema must never leak into this source.
    relations[("other", "table_0")] = ("BASE TABLE", [("x", "integer")])
    relations[("other", "view_x")] = ("VIEW", [("x", "integer")])
    late = {
        (SCHEMA, f"lbv_{i}"): [("customer", "character varying(64)"), ("ltv", "numeric(18,2)")]
        for i in range(n_late_binding)
    }
    late[("other", "lbv_other")] = [("y", "integer")]
    return FakeRedshiftCatalog(relations, late, late_binding_error)


def _discover(client, fallback):
    return client._get_tables_basic() if fallback else client.get_tables()


@pytest.mark.parametrize("fallback", [False, True], ids=["enriched", "basic"])
@pytest.mark.parametrize("n_tables,n_views,n_late", [(2, 1, 1), (0, 3, 0), (1, 0, 2), (3, 2, 4)])
def test_discovery_returns_tables_views_and_late_binding_views(monkeypatch, fallback, n_tables, n_views, n_late):
    client = _client(monkeypatch, _catalog(n_tables, n_views, n_late))

    tables = {t.name: t for t in _discover(client, fallback)}

    expected = (
        {f"table_{i}" for i in range(n_tables)}
        | {f"view_{i}" for i in range(n_views)}
        | {f"lbv_{i}" for i in range(n_late)}
    )
    assert set(tables) == expected
    for t in tables.values():
        assert t.columns, f"{t.name} was discovered without columns"
        assert t.metadata_json["schema"] == SCHEMA
    for i in range(n_late):
        dtypes = [c.dtype for c in tables[f"lbv_{i}"].columns]
        assert all(dtypes), "late-binding view columns must carry a type"


@pytest.mark.parametrize("fallback", [False, True], ids=["enriched", "basic"])
def test_late_binding_lookup_failure_keeps_tables_and_views(monkeypatch, fallback):
    catalog = _catalog(2, 2, 3, late_binding_error=RuntimeError("permission denied"))
    client = _client(monkeypatch, catalog)

    names = {t.name for t in _discover(client, fallback)}

    assert names == {"table_0", "table_1", "view_0", "view_1"}


def test_view_known_to_information_schema_is_not_duplicated(monkeypatch):
    catalog = _catalog(1, 1, 0)
    # A catalog that reports the same view through both sources.
    catalog.late_binding_views[(SCHEMA, "view_0")] = [("day", "date"), ("total", "numeric")]
    client = _client(monkeypatch, catalog)

    tables = client.get_tables()

    view = next(t for t in tables if t.name == "view_0")
    assert [t.name for t in tables].count("view_0") == 1
    assert [c.name for c in view.columns] == ["day", "total"]
