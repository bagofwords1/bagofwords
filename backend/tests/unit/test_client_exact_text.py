"""Every guarded SQL client sends the server exactly the text the guard checked.

Contract: for each client the read-only guard knows, the statement its
driver receives from `execute_query(checked)` is `checked`, the text
`ensure_read_only` returned, byte for byte and once. Nothing on the way
rewrites it: no bind-marker parsing of `:name` or `%`, no comment stripping,
no wrapping.

The boundary is the driver. Each client is a real client class with no
server: its connection factory hands out a fake DBAPI connection (or the
fake session, job or API the driver library exposes) that records what it
is sent, or, for the clients that go through SQLAlchemy, a real SQLAlchemy
connection to an in-memory SQLite database whose cursor executions are
recorded. Pinot runs pinotdb's own connection and cursor over a recording
HTTP transport, since pinotdb itself formats the text. The driver below
SQLAlchemy is not exercised here; the live
exact-text tests cover psycopg2 and pymysql on real servers
(tests/integrations/test_exact_text_send_live.py). Vertica has its own
file (test_vertica_client.py), Postgres goes through the session checks in
the live tests, and the clients listed in `NOT_SQL_TEXT` do not send SQL
text.
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest
import sqlalchemy
from sqlalchemy import event

from app.ai.code_execution.client_profiles import _CLIENT_TABLE
from app.ai.code_execution.sql_guard import ensure_read_only

# Literal text that a bind-marker parser, a `%` formatter or a comment
# stripper would change.
QUERY = "SELECT 'a--b :x %s %% 12:30' AS a FROM orders -- a note :y %s"


class FakeCursor:
    def __init__(self, sent: list):
        self.sent = sent
        self.description = None

    def execute(self, sql, *args, **kwargs):
        self.sent.append(sql)
        self.description = [("a", None, None, None, None, None, None)]
        return self

    def fetchall(self):
        return [("x",)]

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    """A DBAPI connection whose cursors record what they execute."""

    def __init__(self):
        self.sent: list[str] = []

    def cursor(self, *args, **kwargs):
        return FakeCursor(self.sent)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _client(family: str):
    import importlib

    for (module, name), entry in _CLIENT_TABLE.items():
        if entry.family == family:
            return object.__new__(getattr(importlib.import_module(module), name))
    raise KeyError(family)


def _connects_to(client, connection) -> None:
    client.connect = lambda *args, **kwargs: contextlib.nullcontext(connection)


def _dbapi(family: str, **attrs):
    client = _client(family)
    for key, value in attrs.items():
        setattr(client, key, value)
    connection = FakeConnection()
    _connects_to(client, connection)
    return client, connection.sent


def _sqlalchemy(family: str, **attrs):
    """A real SQLAlchemy connection to an in-memory SQLite database that
    holds the table the query reads; every cursor execution is recorded."""
    client = _client(family)
    for key, value in attrs.items():
        setattr(client, key, value)
    engine = sqlalchemy.create_engine("sqlite://", poolclass=sqlalchemy.pool.StaticPool)
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE orders (id INTEGER)")
        conn.exec_driver_sql("INSERT INTO orders VALUES (1)")
    sent: list[str] = []
    event.listen(engine, "before_cursor_execute",
                 lambda conn, cursor, statement, *rest: sent.append(statement))

    @contextlib.contextmanager
    def connect(*args, **kwargs):
        with engine.connect() as conn:
            yield conn

    client.connect = connect
    return client, sent


def _trino(family: str):
    client = _client(family)
    raw = FakeConnection()
    _connects_to(client, SimpleNamespace(connection=SimpleNamespace(dbapi_connection=raw), info={}))
    return client, raw.sent


def _clickhouse():
    client, sent = _client("ClickhouseClient"), []

    def query(sql, *args, **kwargs):
        sent.append(sql)
        return SimpleNamespace(result_set=[("x",)], column_names=["a"])

    _connects_to(client, SimpleNamespace(query=query))
    return client, sent


def _bigquery():
    client, sent = _client("BigqueryClient"), []
    client.maximum_bytes_billed, client.use_query_cache = None, True

    def query(sql, job_config=None, **kwargs):
        sent.append(sql)
        frame = pd.DataFrame({"a": ["x"]})
        return SimpleNamespace(result=lambda: SimpleNamespace(to_dataframe=lambda: frame))

    _connects_to(client, SimpleNamespace(query=query))
    return client, sent


def _athena(monkeypatch):
    from app.data_sources.clients import aws_athena_client

    client, sent = _client("AwsAthenaClient"), []
    for key in ("database", "workgroup", "data_source", "s3_output_location", "encryption_option", "kms_key",
                "boto3_session"):
        setattr(client, key, None)
    client.result_reuse_enable = False
    client._multi = False  # one database: the multi-database table checks do not apply

    def read_sql_query(sql, **kwargs):
        sent.append(sql)
        return pd.DataFrame({"a": ["x"]})

    monkeypatch.setattr(aws_athena_client, "wr", SimpleNamespace(athena=SimpleNamespace(read_sql_query=read_sql_query)))
    return client, sent


def _spark():
    client, sent = _client("SparkConnectClient"), []
    client.require_partition_filter, client.max_scan_bytes = False, None

    def sql(text):
        sent.append(text)
        return SimpleNamespace(toPandas=lambda: pd.DataFrame({"a": ["x"]}))

    _connects_to(client, SimpleNamespace(sql=sql))
    return client, sent


def _duckdb():
    """A real in-process DuckDB connection, recorded at its execute call.
    The client is configured over one URI, so `orders` is its view."""
    client, sent = _client("DuckDBClient"), []
    client.__init__(uris="/data/orders.csv")
    con = duckdb.connect()
    con.execute("CREATE TABLE orders (id INT)")

    class Recording:
        def execute(self, sql, *args):
            sent.append(sql)
            return con.execute(sql, *args)

    _connects_to(client, Recording())
    return client, sent


def _sqlite():
    """A real sqlite3 connection; its trace callback records each statement."""
    client, sent = _client("SqliteClient"), []
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE orders (id INTEGER)")
    con.set_trace_callback(sent.append)
    _connects_to(client, con)
    return client, sent


def _timbr():
    client, sent = _client("TimbrClient"), []
    client._base_url, client.ontology = "https://timbr.invalid/timbr/api", "shop"

    def post(url, json=None, timeout=None, **kwargs):
        sent.append(json["query"])
        return SimpleNamespace(status_code=200, text="", json=lambda: {"status": "success", "data": [{"a": "x"}]})

    client._get_session = lambda: SimpleNamespace(post=post)
    return client, sent


def _pinot(query_options=None):
    """Pinot: pinotdb's own connection and cursor, over an HTTP transport
    that records the SQL each request carries and answers as a broker does."""
    import json

    import httpx

    from app.data_sources.clients.pinot_client import PinotClient

    sent: list[str] = []

    def broker(request):
        sent.append(json.loads(request.content)["sql"])
        return httpx.Response(200, json={
            "resultTable": {"dataSchema": {"columnNames": ["a"], "columnDataTypes": ["STRING"]}, "rows": [["x"]]},
            "exceptions": [], "numServersQueried": 1, "numServersResponded": 1, "timeUsedMs": 1,
        })

    client = PinotClient(host="broker.invalid", port=8099, secure=False, query_options=query_options)
    client._connect_kwargs["session"] = httpx.Client(transport=httpx.MockTransport(broker))
    return client, sent


def _ibmi():
    """IBM i: a fake Mapepire SQL job that records each query it is given."""
    client, sent = _client("IbmiClient"), []
    result = SimpleNamespace(metadata=SimpleNamespace(columns=[SimpleNamespace(name="A")]), data=[["x"]],
                             is_done=True)

    def query(sql, options=None):
        sent.append(sql)
        return SimpleNamespace(run=lambda rows_to_fetch=None: result, close=lambda: None)

    _connects_to(client, SimpleNamespace(query=query))
    return client, sent


def _hive():
    """Hive / Impala / Spark Thrift: a fake HiveServer2 connection whose
    cursor records each statement."""
    client, sent = _client("HiveClient"), []
    client.engine, client.max_rows = "hive", 10

    class Cursor(FakeCursor):
        def fetchmany(self, size):
            rows, self.rows = getattr(self, "rows", [("x",)]), []
            return rows

    client._conn = SimpleNamespace(cursor=lambda: Cursor(sent))
    return client, sent


def _dremio():
    """Dremio: its REST job API, recorded at the request the client builds."""
    client, sent = _client("DremioClient"), []
    client.max_rows, client.timeout_seconds = 10, 30

    def request(method, path, **kwargs):
        if (method, path) == ("POST", "/sql"):
            sent.append(kwargs["json"]["sql"])
            return {"id": "job-1"}
        if path == "/job/job-1":
            return {"jobState": "COMPLETED", "rowCount": 1}
        return {"schema": [{"name": "a"}], "rows": [{"a": "x"}]}

    client._request = request
    return client, sent


RIGS = {
    "AwsAthenaClient": _athena,
    "DremioClient": lambda mp: _dremio(),
    "HiveClient": lambda mp: _hive(),
    "AwsRedshiftClient": lambda mp: _dbapi("AwsRedshiftClient"),
    "BigqueryClient": lambda mp: _bigquery(),
    "ClickhouseClient": lambda mp: _clickhouse(),
    "DatabricksSqlClient": lambda mp: _dbapi("DatabricksSqlClient"),
    "Db2Client": lambda mp: _sqlalchemy("Db2Client"),
    "DruidClient": lambda mp: _dbapi("DruidClient"),
    "DuckDBClient": lambda mp: _duckdb(),
    "IbmiClient": lambda mp: _ibmi(),
    "MariadbClient": lambda mp: _sqlalchemy("MariadbClient"),
    "MSSQLClient": lambda mp: _sqlalchemy("MSSQLClient"),
    "MsFabricClient": lambda mp: _dbapi("MsFabricClient"),
    "MysqlClient": lambda mp: _sqlalchemy("MysqlClient"),
    "OracledbClient": lambda mp: _sqlalchemy("OracledbClient"),
    "PinotClient": lambda mp: _pinot(),
    "PrestoClient": lambda mp: _trino("PrestoClient"),
    "SapHanaClient": lambda mp: _dbapi("SapHanaClient"),
    "SnowflakeClient": lambda mp: _sqlalchemy("SnowflakeClient"),
    "SparkConnectClient": lambda mp: _spark(),
    "SqliteClient": lambda mp: _sqlite(),
    "SybaseClient": lambda mp: _dbapi("SybaseClient"),
    "TeradataClient": lambda mp: _sqlalchemy("TeradataClient"),
    "TimbrClient": lambda mp: _timbr(),
    "TrinoClient": lambda mp: _trino("TrinoClient"),
}
# pd.read_sql first asks the database whether the text names a table (a
# catalog lookup that sends the text as a value, not as a statement), then
# runs the text.
LOOKS_UP_A_TABLE_FIRST = {"Db2Client", "TeradataClient"}
COVERED_ELSEWHERE = {
    "VerticaClient": "tests/unit/test_vertica_client.py",
    "PostgresqlClient": "tests/integrations/test_exact_text_send_live.py",
    # Iceberg names tables namespace.table: QUERY's `orders` is not one.
    "IcebergClient": "test_iceberg_runs_exactly_the_checked_text, below",
}
NOT_SQL_TEXT = {
    "SplunkClient": "SPL: the client sends `search <text>`, the form check_spl reads",
    "Neo4jClient": "Cypher with driver-bound $name parameters",
}


def test_every_guarded_client_is_covered():
    guarded = {e.family for e in _CLIENT_TABLE.values() if e.profile is not None}
    assert guarded == set(RIGS) | set(COVERED_ELSEWHERE) | set(NOT_SQL_TEXT)


@pytest.mark.parametrize("family", sorted(RIGS))
def test_the_driver_receives_exactly_the_checked_text(family, monkeypatch):
    client, sent = RIGS[family](monkeypatch)
    checked = ensure_read_only(client, QUERY)
    client.execute_query(checked)
    if family in LOOKS_UP_A_TABLE_FIRST:
        assert sent[-1] == checked and sent.count(checked) == 1
    else:
        assert sent == [checked]


def test_iceberg_runs_exactly_the_checked_text(monkeypatch):
    """Iceberg runs the checked text on an in-memory DuckDB after registering
    each catalog table it names as a view (its own CREATE statements): the
    query itself is executed once, byte for byte."""
    import pyarrow as pa

    query = "SELECT 'a--b :x %s %% 12:30' AS a FROM shop.orders -- a note :y %s"
    client = _client("IcebergClient")
    client.max_scan_rows = 1000
    scan = SimpleNamespace(plan_files=lambda: [], to_arrow=lambda: pa.table({"id": [1]}))
    table = SimpleNamespace(schema=lambda: SimpleNamespace(fields=[SimpleNamespace(name="id")]),
                            scan=lambda **kwargs: scan)
    client._table_identifiers = lambda: [("shop", "orders")]
    client._cat = lambda: SimpleNamespace(load_table=lambda ident: table)
    sent: list[str] = []
    real_connect = duckdb.connect

    class Recording:
        def __init__(self, con):
            self.con = con

        def execute(self, sql, *args):
            sent.append(sql)
            return self.con.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.con, name)

    checked = ensure_read_only(client, query)
    # Only the client's own `import duckdb` sees the recording module; the
    # DuckDB guard keeps its module (and the connection it builds) to itself.
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, "duckdb", SimpleNamespace(
            connect=lambda *a, **k: Recording(real_connect(*a, **k))))
        client.execute_query(checked)
    assert sent[-1] == checked and sent.count(checked) == 1
    assert all(s.startswith("CREATE ") for s in sent[:-1])


@pytest.mark.parametrize("query_options", [None, "timeoutMs=1000"])
@pytest.mark.parametrize("sql", [
    "SELECT * FROM orders WHERE note LIKE '10%%'",
    "SELECT * FROM orders WHERE note = '%(x)s' OR code LIKE '%'",
])
def test_pinot_sends_percent_signs_as_written(sql, query_options):
    # pinotdb formats the text it is given with Python's % operator: '%%'
    # became '%' and '%(' raised. With no parameters the client skips that.
    client, sent = _pinot(query_options=query_options)
    df = client.execute_query(ensure_read_only(client, sql))
    assert sent == [sql]
    assert df.to_dict("records") == [{"a": "x"}]


def test_awswrangler_leaves_text_without_params_unchanged():
    # The Athena client passes no params, so awswrangler formats nothing.
    from awswrangler.athena._utils import _apply_formatter

    assert _apply_formatter(QUERY, None, "named") == (QUERY, None)
