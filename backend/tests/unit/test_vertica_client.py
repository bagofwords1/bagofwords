"""Vertica: the text the read-only guard checked is the text the server gets.

Contract: `VerticaClient.execute_query` sends exactly what
`ensure_read_only` returned, once, on the connection's vertica_python
cursor, and builds the DataFrame the way it always has (columns from the
cursor, values as the driver returns them, every row, an empty result with
no columns).

The boundary is the driver: a vertica_python cursor is faked and installed
as the connection verticapy hands out, so verticapy and the client run as
they do in production. verticapy's vDataFrame rewrites a query before it
sends it (it removes what it takes for comments, joins lines and wraps the
text in a subquery), so the client no longer sends queries through it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from vertica_python.vertica.cursor import Cursor as VerticaCursor
from verticapy.connection.global_connection import get_global_connection

from app.ai.code_execution.sql_guard import ensure_read_only
from app.data_sources.clients.vertica_client import VerticaClient

NL = chr(10)


class _Column(tuple):
    """A cursor description entry, as vertica_python's Column reads."""

    type_name = "Varchar"


class FakeCursor(VerticaCursor):
    """A vertica_python cursor that records every text it is sent and
    answers each with the same columns and rows."""

    def __init__(self, columns, rows):
        self.sent: list[str] = []
        self._columns, self._rows = columns, rows
        self.description = None

    def execute(self, operation, parameters=None, *args, **kwargs):
        self.sent.append(operation)
        self.description = [_Column((name, None, 10, None, 0, 0, None)) for name in self._columns]
        return self

    def fetchall(self):
        return [list(row) for row in self._rows]

    def fetchone(self):
        return list(self._rows[0]) if self._rows else None

    def closed(self):
        return False

    def close(self):
        pass


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def closed(self):
        return False

    def cursor(self, *args, **kwargs):
        return self._cursor

    def close(self):
        pass


@pytest.fixture
def driver():
    """Install a fake driver connection as verticapy's current one; yield
    a function that builds the cursor and a connected client."""
    connection = get_global_connection()
    previous = connection.get_connection()

    def install(columns=("a",), rows=((1,),)):
        cursor = FakeCursor(list(columns), list(rows))
        connection.set_connection(FakeConnection(cursor))
        client = object.__new__(VerticaClient)
        client._connected, client._connection_name = True, "vertica_conn_test"
        return client, cursor

    yield install
    connection.set_connection(previous)


QUERY = "SELECT id, name -- it's a note" + NL + ", amount FROM orders WHERE name = 'a--b' AND code = '$1'"


def test_the_driver_receives_exactly_the_checked_text_once(driver):
    client, cursor = driver(columns=("id", "name", "amount"), rows=[(1, "a--b", Decimal("1.5"))])
    checked = ensure_read_only(client, QUERY)
    client.execute_query(checked)
    assert cursor.sent == [checked]


def test_rows_and_columns_come_back_as_the_driver_returns_them(driver):
    client, _ = driver(columns=("id", "name", "amount"), rows=[(1, "a", Decimal("1.5")), (2, None, None)])
    df = client.execute_query("SELECT id, name, amount FROM orders")
    assert list(df.columns) == ["id", "name", "amount"]
    assert [str(t) for t in df.dtypes] == ["int64", "object", "object"]
    assert df.to_dict("records") == [{"id": 1, "name": "a", "amount": Decimal("1.5")},
                                     {"id": 2, "name": None, "amount": None}]


def test_an_empty_result_is_an_empty_frame_with_no_columns_as_before(driver):
    client, _ = driver(columns=("id", "name"), rows=[])
    df = client.execute_query("SELECT id, name FROM orders WHERE 1 = 0")
    assert df.shape == (0, 0)


def test_every_row_comes_back_and_repeated_column_names_are_kept(driver):
    client, _ = driver(columns=("a", "a", "b"), rows=[(i, i * 0.5, i % 2 == 0) for i in range(5000)])
    df = client.execute_query("SELECT a, a, b FROM t")
    assert df.shape == (5000, 3)
    assert list(df.columns) == ["a", "a", "b"]
    assert [str(t) for t in df.dtypes] == ["int64", "float64", "bool"]
