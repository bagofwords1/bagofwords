"""SQLite catalog discovery must list every table the file holds, whatever its name.

SQLite accepts any string as a quoted identifier, and real-world files use that
freely: spaces ("Order Details"), hyphens, non-ASCII (Hebrew, accented Latin)
and even embedded quotes. Discovery used to drop every name that was not a bare
ASCII identifier, so those tables silently vanished from the agent's schema.
The table name is data read from sqlite_master; it must never be able to change
the SQL that reads its columns.
"""
import sqlite3

import pytest

from app.data_sources.clients.sqlite_client import SqliteClient

# Each shape that a bare-identifier rule rejects, plus a control that it accepts.
TABLE_NAMES = [
    "customers",
    "Order Details",
    "sales-2024",
    "הזמנות",
    "café_ventes",
    'odd"quote',
    "x'); DROP TABLE customers; --",
]


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "catalog.db"
    conn = sqlite3.connect(path)
    for i, name in enumerate(TABLE_NAMES):
        # A per-table column name proves columns are read from the right table.
        conn.execute(f"CREATE TABLE {_quote(name)} (id INTEGER, col_{i} TEXT)")
    conn.commit()
    conn.close()
    return SqliteClient(database=str(path)), path


def test_every_table_is_discovered_with_its_own_columns(client):
    sqlite_client, _ = client

    by_name = {t.name: t for t in sqlite_client.get_tables()}

    assert set(by_name) == set(TABLE_NAMES)
    for i, name in enumerate(TABLE_NAMES):
        columns = {(c.name, c.dtype) for c in by_name[name].columns}
        assert columns == {("id", "INTEGER"), (f"col_{i}", "TEXT")}, name


def test_a_hostile_table_name_does_not_alter_the_database(client):
    sqlite_client, path = client

    sqlite_client.get_tables()

    conn = sqlite3.connect(path)
    try:
        remaining = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert remaining == set(TABLE_NAMES)
