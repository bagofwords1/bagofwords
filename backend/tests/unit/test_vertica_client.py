"""Vertica: each query runs on a connection of its own client, as checked.

Contract: `VerticaClient.execute_query` opens a vertica_python connection
with that client's own connection details, sends exactly what
`ensure_read_only` returned, once, on one cursor, and closes the connection,
on success, on an error (which it re-raises) and after a call the wrapper's
timeout gave up on. It builds the DataFrame the way it always has (columns
from the cursor, values as the driver returns them, every row, an empty
result with no columns). Catalog reads and the connection test use the same
per-call connection. Nothing is kept between calls and no credentials file
is written, so two clients never share a connection.

The boundary is the driver: `vertica_python.connect` is replaced by a fake
that hands out recording connections; the client's own code runs as in
production. (verticapy kept one connection for the whole process, replaced
by every client that connected, and its vDataFrame rewrote a query before
sending it, so the client no longer uses it.)
"""

from __future__ import annotations

import threading
import time
from decimal import Decimal

import pytest
import vertica_python

from app.ai.code_execution.code_execution import QueryCapturingClientWrapper, QueryTimeoutError
from app.ai.code_execution.sql_guard import ensure_read_only
from app.data_sources.clients.vertica_client import VerticaClient

NL = chr(10)


class FakeCursor:
    """Records every text it is sent and answers each with the same
    columns and rows (after `delay` seconds), or raises `error`."""

    def __init__(self, connection):
        self.connection = connection
        self.description = None

    def execute(self, operation, parameters=None, *args, **kwargs):
        server = self.connection.server
        self.connection.sent.append(operation)
        if server.delay:
            time.sleep(server.delay)
        if server.error is not None:
            raise server.error
        self.description = [(name, None, 10, None, 0, 0, None) for name in server.columns]
        return self

    def fetchall(self):
        return [list(row) for row in self.connection.server.rows]

    def close(self):
        pass


class FakeConnection:
    def __init__(self, server, params):
        self.server, self.params = server, params
        self.sent: list[str] = []
        self.released = threading.Event()

    def cursor(self, *args, **kwargs):
        return FakeCursor(self)

    def closed(self):
        return self.released.is_set()

    def close(self):
        self.released.set()


class FakeServer:
    """What `vertica_python.connect` reaches: every connection opened, with
    the details it was opened with."""

    def __init__(self):
        self.connections: list[FakeConnection] = []
        self.columns, self.rows = ["a"], [(1,)]
        self.delay, self.error = 0.0, None

    def connect(self, **params):
        connection = FakeConnection(self, params)
        self.connections.append(connection)
        return connection


@pytest.fixture
def server(monkeypatch, tmp_path):
    fake = FakeServer()
    monkeypatch.setattr(vertica_python, "connect", fake.connect)
    # A credentials file would be written under the home directory.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("VERTICAPY_CONNECTION", raising=False)
    return fake


def _client(host="vertica-a.invalid", database="sales", user="reader"):
    return VerticaClient(host=host, port=5433, database=database, user=user, password="pw")


QUERY = "SELECT id, name -- it's a note" + NL + ", amount FROM orders WHERE name = 'a--b' AND code = '$1'"


def test_the_driver_receives_exactly_the_checked_text_once(server):
    server.columns, server.rows = ["id", "name", "amount"], [(1, "a--b", Decimal("1.5"))]
    client = _client()
    checked = ensure_read_only(client, QUERY)
    client.execute_query(checked)
    assert [c.sent for c in server.connections] == [[checked]]


def test_each_query_runs_on_a_connection_of_its_own_client_that_is_closed_after_it(server):
    a, b = _client(host="vertica-a.invalid", database="sales"), _client(host="vertica-b.invalid", database="hr")
    for client in (a, b, a):
        client.execute_query("SELECT 1")
    assert [(c.params["host"], c.params["database"]) for c in server.connections] == [
        ("vertica-a.invalid", "sales"), ("vertica-b.invalid", "hr"), ("vertica-a.invalid", "sales")]
    assert all(c.released.is_set() and c.sent == ["SELECT 1"] for c in server.connections)


def test_a_connection_is_opened_with_the_details_verticapy_used(server):
    _client().execute_query("SELECT 1")
    assert server.connections[0].params == {
        "host": "vertica-a.invalid", "port": 5433, "database": "sales", "user": "reader", "password": "pw",
        "unicode_error": "ignore",
    }


def test_no_credentials_file_is_written(server, tmp_path):
    client = _client()
    client.execute_query("SELECT 1")
    client.test_connection()
    assert list(tmp_path.iterdir()) == []


def test_a_query_error_is_raised_and_the_connection_closed(server):
    server.error = vertica_python.errors.DatabaseError("Relation does not exist")
    with pytest.raises(vertica_python.errors.DatabaseError, match="does not exist") as raised:
        _client().execute_query("SELECT * FROM missing")
    assert raised.value is server.error
    assert server.connections[0].released.is_set()


def test_a_query_past_the_wrapper_timeout_times_out_as_before_and_its_connection_is_closed(server):
    server.delay = 2.0
    wrapper = QueryCapturingClientWrapper(
        original_client=_client(), captured_queries=[], captured_timings=[], usage_context=None,
        client_key="main", query_timeout_seconds=1,
    )
    started = time.monotonic()
    with pytest.raises(QueryTimeoutError):
        wrapper.execute_query("SELECT 1")
    assert 0.9 <= time.monotonic() - started < 2.0
    # The abandoned call still closes its own connection when it ends.
    assert server.connections[0].released.wait(5)


def test_rows_and_columns_come_back_as_the_driver_returns_them(server):
    server.columns, server.rows = ["id", "name", "amount"], [(1, "a", Decimal("1.5")), (2, None, None)]
    df = _client().execute_query("SELECT id, name, amount FROM orders")
    assert list(df.columns) == ["id", "name", "amount"]
    assert [str(t) for t in df.dtypes] == ["int64", "object", "object"]
    assert df.to_dict("records") == [{"id": 1, "name": "a", "amount": Decimal("1.5")},
                                     {"id": 2, "name": None, "amount": None}]


def test_an_empty_result_is_an_empty_frame_with_no_columns_as_before(server):
    server.columns, server.rows = ["id", "name"], []
    df = _client().execute_query("SELECT id, name FROM orders WHERE 1 = 0")
    assert df.shape == (0, 0)


def test_every_row_comes_back_and_repeated_column_names_are_kept(server):
    server.columns, server.rows = ["a", "a", "b"], [(i, i * 0.5, i % 2 == 0) for i in range(5000)]
    df = _client().execute_query("SELECT a, a, b FROM t")
    assert df.shape == (5000, 3)
    assert list(df.columns) == ["a", "a", "b"]
    assert [str(t) for t in df.dtypes] == ["int64", "float64", "bool"]


def test_the_catalog_is_read_on_a_connection_of_the_client_s_own(server):
    server.columns = ["table_schema", "table_name", "object_type", "column_name", "data_type", "column_comment",
                      "table_comment"]
    server.rows = [("public", "orders", "TABLE", "id", "int", None, "Orders"),
                   ("public", "orders", "TABLE", "note", "varchar(80)", "Free text", "Orders")]
    tables = _client().get_tables()
    assert [(t.name, t.description, [(c.name, c.dtype, c.description) for c in t.columns]) for t in tables] == [
        ("orders", "Orders", [("id", "int", None), ("note", "varchar(80)", "Free text")])]
    assert len(server.connections) == 1 and server.connections[0].released.is_set()
    assert "v_catalog.tables" in server.connections[0].sent[0]


def test_the_connection_test_runs_on_a_connection_of_the_client_s_own(server):
    assert _client().test_connection()["success"] is True
    assert server.connections[0].sent == ["SELECT 1 as test"] and server.connections[0].released.is_set()


def test_a_failed_connect_is_raised_with_the_driver_error_as_its_cause(server, monkeypatch):
    refused = vertica_python.errors.ConnectionError("refused")

    def connect(**params):
        raise refused

    monkeypatch.setattr(vertica_python, "connect", connect)
    with pytest.raises(RuntimeError, match="Failed to connect to Vertica") as raised:
        _client().execute_query("SELECT 1")
    assert raised.value.__cause__ is refused


def test_a_failed_connect_names_the_driver_error_by_type_only(server, monkeypatch):
    # A driver message may hold connection details; the cause keeps it.
    def connect(**params):
        raise vertica_python.errors.ConnectionError("host vertica-a.invalid user reader password=pw refused")

    monkeypatch.setattr(vertica_python, "connect", connect)
    with pytest.raises(RuntimeError) as raised:
        _client().execute_query("SELECT 1")
    assert str(raised.value) == "Failed to connect to Vertica (ConnectionError)"


def test_a_catalog_read_that_falls_back_to_the_basic_one_is_logged_by_type_only(server, monkeypatch, caplog):
    import logging

    execute = FakeCursor.execute

    def refuse_comments(self, operation, *args, **kwargs):
        if "v_catalog.comments" in operation:
            raise vertica_python.errors.ProgrammingError("Permission denied for v_catalog.comments password=pw")
        return execute(self, operation, *args, **kwargs)

    monkeypatch.setattr(FakeCursor, "execute", refuse_comments)
    server.columns = ["table_schema", "table_name", "object_type", "column_name", "data_type"]
    server.rows = [("public", "orders", "TABLE", "id", "int")]
    logger = logging.getLogger("app.data_sources.clients.vertica_client")
    logger.disabled = False
    with caplog.at_level(logging.WARNING, logger=logger.name):
        tables = _client().get_tables()
    assert [(t.name, [c.name for c in t.columns]) for t in tables] == [("orders", ["id"])]
    assert [r.getMessage() for r in caplog.records] == [
        "vertica: reading the catalog with comments failed (ProgrammingError); reading it without them"]


def test_a_failed_close_is_logged_by_type_only_and_the_result_still_returned(server, monkeypatch, caplog):
    import logging

    def close(self):
        raise vertica_python.errors.ConnectionError("password=pw lost")

    monkeypatch.setattr(FakeConnection, "close", close)
    logger = logging.getLogger("app.data_sources.clients.vertica_client")
    logger.disabled = False
    with caplog.at_level(logging.WARNING, logger=logger.name):
        df = _client().execute_query("SELECT 1")
    assert df.to_dict("records") == [{"a": 1}]
    assert [r.getMessage() for r in caplog.records] == [
        "vertica: closing the connection failed (ConnectionError)"]


def test_a_failed_query_is_logged_by_type_only_and_nothing_is_printed(server, monkeypatch, caplog, capsys):
    import logging

    server.error = vertica_python.errors.ProgrammingError("password=pw denied")
    logger = logging.getLogger("app.data_sources.clients.vertica_client")
    logger.disabled = False
    with caplog.at_level(logging.WARNING, logger=logger.name), pytest.raises(vertica_python.errors.ProgrammingError):
        _client().execute_query("SELECT 1")
    assert capsys.readouterr().out == ""
    assert [r.getMessage() for r in caplog.records] == ["vertica: the query failed (ProgrammingError)"]


def test_a_failed_catalog_read_is_logged_by_type_only_and_nothing_is_printed(server, monkeypatch, caplog, capsys):
    import logging

    server.error = RuntimeError("password=pw denied")
    logger = logging.getLogger("app.data_sources.clients.vertica_client")
    logger.disabled = False
    with caplog.at_level(logging.WARNING, logger=logger.name):
        assert _client().get_tables() == []
    assert capsys.readouterr().out == ""
    messages = [r.getMessage() for r in caplog.records]
    assert messages[-1] == "vertica: reading the catalog failed (RuntimeError)"
    assert not any("password" in m for m in messages)


def test_the_description_names_the_driver_the_client_uses():
    text = _client().description
    assert "verticapy" not in text and "vertica_python" in text
