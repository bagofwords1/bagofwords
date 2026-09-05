"""Unit coverage for `attach_foreign_keys`, without Docker.

The container tests in `tests/integrations/test_fk_reflection.py` are the real
proof, but they skip wherever no Docker daemon is reachable — which includes a
default CI run. These use SQLite so the helper's contract is checked on every
run: names composed through `name_fn`, composite constraints flattened per
column pair, tables without constraints left alone.

SQLite earns its place beyond convenience: it reports `referred_schema` as
None for every constraint, which is exactly the shape that made PostgreSQL
mis-resolve cross-schema references, so the fallback path is exercised here
rather than only under Docker.
"""

from __future__ import annotations

import pytest
import sqlalchemy

from app.ai.prompt_formatters import Table, TableColumn
from app.data_sources.fk_reflection import attach_foreign_keys

SCHEMA = [
    "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT)",
    "CREATE TABLE regions (code TEXT, sub_code TEXT, label TEXT, PRIMARY KEY (code, sub_code))",
    """CREATE TABLE orders (
        id INTEGER PRIMARY KEY,
        customer_id INTEGER REFERENCES customers(id),
        region_code TEXT,
        region_sub_code TEXT,
        FOREIGN KEY (region_code, region_sub_code) REFERENCES regions(code, sub_code)
    )""",
    "CREATE TABLE audit_log (id INTEGER PRIMARY KEY, message TEXT)",
]

COLUMNS = {
    "customers": [("id", "INTEGER"), ("name", "TEXT")],
    "regions": [("code", "TEXT"), ("sub_code", "TEXT"), ("label", "TEXT")],
    "orders": [
        ("id", "INTEGER"),
        ("customer_id", "INTEGER"),
        ("region_code", "TEXT"),
        ("region_sub_code", "TEXT"),
    ],
    "audit_log": [("id", "INTEGER"), ("message", "TEXT")],
}


@pytest.fixture
def reflected():
    """Yield (connection, tables) shaped the way a client hands them over."""
    engine = sqlalchemy.create_engine("sqlite://")
    with engine.connect() as conn:
        for stmt in SCHEMA:
            conn.execute(sqlalchemy.text(stmt))
        conn.commit()

        tables = {
            ("main", name): Table(
                name=f"main.{name}",
                columns=[TableColumn(name=c, dtype=d) for c, d in cols],
                pks=[],
                fks=[],
            )
            for name, cols in COLUMNS.items()
        }
        yield conn, tables
    engine.dispose()


def _edges(table: Table) -> set[tuple[str, str, str]]:
    return {
        (fk.column.name, fk.references_name, fk.references_column.name)
        for fk in (table.fks or [])
    }


def test_edges_are_attached_using_the_callers_naming_convention(reflected):
    conn, tables = reflected
    attached = attach_foreign_keys(
        conn, tables, None, lambda s, t: f"{s}.{t}"
    )

    assert attached == 3  # one single-column + one two-column constraint
    assert _edges(tables[("main", "orders")]) == {
        ("customer_id", "main.customers", "id"),
        ("region_code", "main.regions", "code"),
        ("region_sub_code", "main.regions", "sub_code"),
    }


def test_name_fn_fully_controls_the_reference_string(reflected):
    """A client that names tables bare must get bare references back.

    The reference is matched downstream by string equality against the table's
    own name, so the helper must never impose a qualifier the client doesn't
    use.
    """
    conn, tables = reflected
    for key, table in tables.items():
        table.name = key[1]

    attach_foreign_keys(conn, tables, None, lambda s, t: t)

    assert _edges(tables[("main", "orders")]) == {
        ("customer_id", "customers", "id"),
        ("region_code", "regions", "code"),
        ("region_sub_code", "regions", "sub_code"),
    }


def test_every_reference_names_a_table_that_exists(reflected):
    conn, tables = reflected
    attach_foreign_keys(conn, tables, None, lambda s, t: f"{s}.{t}")

    known = {t.name for t in tables.values()}
    for table in tables.values():
        for fk in table.fks or []:
            assert fk.references_name in known


def test_tables_without_constraints_are_left_empty(reflected):
    conn, tables = reflected
    attach_foreign_keys(conn, tables, None, lambda s, t: f"{s}.{t}")

    assert tables[("main", "audit_log")].fks == []
    assert tables[("main", "customers")].fks == []


def test_dtypes_are_borrowed_from_the_collected_columns(reflected):
    """Reflection returns column names only; types come from the caller's rows."""
    conn, tables = reflected
    attach_foreign_keys(conn, tables, None, lambda s, t: f"{s}.{t}")

    fk = next(
        fk
        for fk in tables[("main", "orders")].fks
        if fk.column.name == "customer_id"
    )
    assert fk.column.dtype == "INTEGER"
    assert fk.references_column.dtype == "INTEGER"


def test_reflection_failure_costs_the_caller_nothing(reflected):
    """A broken connection must not lose the tables the caller already has.

    Schema sync exists to return tables; relationships are an enrichment. If
    introspection can throw away the primary result, one unlucky permission
    error empties an agent's catalog.
    """
    conn, tables = reflected
    conn.close()

    attached = attach_foreign_keys(conn, tables, None, lambda s, t: f"{s}.{t}")

    assert attached == 0
    assert len(tables) == 4
    assert all(t.columns for t in tables.values())


def test_empty_input_is_not_an_error(reflected):
    conn, _ = reflected
    assert attach_foreign_keys(conn, {}, None, lambda s, t: t) == 0
