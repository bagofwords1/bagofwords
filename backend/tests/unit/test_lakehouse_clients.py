"""Unit tests for the Iceberg, Dremio and Hive/Impala clients.

Iceberg runs end to end here without Docker: PyIceberg's SQL catalog over a
temp-dir warehouse is a real Iceberg catalog, so planning, pruning, time
travel and the DuckDB execution path are exercised for real. Dremio's REST
API and HiveServer2 are stubbed at the HTTP / cursor boundary; the live
servers are covered in tests/integrations/test_lakehouse_live.py.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from app.ai.code_execution.sql_guard import ReadOnlyViolation, dialect_for_client

# ================================================================ Iceberg


@pytest.fixture(scope="module")
def iceberg_catalog(tmp_path_factory):
    pa = pytest.importorskip("pyarrow")
    from pyiceberg.catalog.sql import SqlCatalog
    from pyiceberg.partitioning import PartitionField, PartitionSpec
    from pyiceberg.schema import Schema
    from pyiceberg.transforms import MonthTransform
    from pyiceberg.types import DoubleType, LongType, NestedField, StringType, TimestampType

    wh = tmp_path_factory.mktemp("wh")
    uri = f"sqlite:///{wh}/catalog.db"
    # The SQL catalog keys its rows by catalog name; the client loads it as "bow".
    cat = SqlCatalog("bow", uri=uri, warehouse=f"file://{wh}")
    cat.create_namespace("sales")
    schema = Schema(
        NestedField(1, "order_id", LongType(), required=False),
        NestedField(2, "region", StringType(), required=False, doc="Sales region"),
        NestedField(3, "amount", DoubleType(), required=False),
        NestedField(4, "order_ts", TimestampType(), required=False),
    )
    spec = PartitionSpec(PartitionField(source_id=4, field_id=1000, transform=MonthTransform(), name="m"))
    orders = cat.create_table(("sales", "orders"), schema=schema, partition_spec=spec)

    def rows(lo, hi):
        return pa.Table.from_pylist([
            {"order_id": i, "region": ["EMEA", "AMER", "APAC"][i % 3], "amount": float(i),
             "order_ts": dt.datetime(2025, 1 + i % 6, 1 + i % 27)} for i in range(lo, hi)
        ], schema=orders.schema().as_arrow())

    orders.append(rows(0, 60))
    orders.append(rows(60, 90))
    regions = cat.create_table(("sales", "regions"), schema=Schema(
        NestedField(1, "region", StringType(), required=False), NestedField(2, "manager", StringType(), required=False)))
    regions.append(pa.Table.from_pylist([{"region": "EMEA", "manager": "Ana"}, {"region": "AMER", "manager": "Bo"},
                                         {"region": "APAC", "manager": "Cy"}], schema=regions.schema().as_arrow()))
    return {"uri": uri, "warehouse": f"file://{wh}", "orders": orders}


def _ice(cat, **kw):
    from app.data_sources.clients.iceberg_client import IcebergClient

    props = json.dumps({"type": "sql", "uri": cat["uri"], "warehouse": cat["warehouse"]})
    client = IcebergClient(catalog_type="rest", uri="http://unused", extra_properties=props, **kw)
    return client


def test_iceberg_catalog_describes_partitioning_and_rows(iceberg_catalog):
    tables = {t.name: t for t in _ice(iceberg_catalog).get_schemas()}
    assert set(tables) == {"sales.orders", "sales.regions"}
    orders = tables["sales.orders"]
    assert "month(order_ts)" in orders.description
    assert "90 rows" in orders.description
    assert {c.name: c.description for c in orders.columns}["region"] == "Sales region"
    assert orders.metadata_json["iceberg"]["partition"] == ["month(order_ts)"]


def test_iceberg_sql_aggregates_and_joins(iceberg_catalog):
    c = _ice(iceberg_catalog)
    df = c.execute_query("SELECT region, COUNT(*) AS n FROM sales.orders GROUP BY region ORDER BY region")
    assert dict(zip(df.region, df.n, strict=True)) == {"AMER": 30, "APAC": 30, "EMEA": 30}
    df = c.execute_query(
        "SELECT r.manager, SUM(o.amount) AS total FROM sales.orders o JOIN sales.regions r "
        "ON o.region = r.region GROUP BY 1 ORDER BY 1")
    assert list(df.manager) == ["Ana", "Bo", "Cy"]
    assert float(df.total.sum()) == float(sum(range(90)))


def test_iceberg_ctes_are_not_mistaken_for_tables(iceberg_catalog):
    df = _ice(iceberg_catalog).execute_query(
        "WITH big AS (SELECT * FROM sales.orders WHERE amount >= 45) SELECT COUNT(*) AS n FROM big")
    assert int(df.n[0]) == 45


def test_iceberg_time_travel(iceberg_catalog):
    c = _ice(iceberg_catalog)
    first = iceberg_catalog["orders"].snapshots()[0]
    assert int(c.execute_query("SELECT COUNT(*) AS n FROM sales.orders", snapshot_id=first.snapshot_id).n[0]) == 60
    as_of = dt.datetime.fromtimestamp(first.timestamp_ms / 1000, dt.UTC).isoformat()
    assert int(c.execute_query("SELECT COUNT(*) AS n FROM sales.orders", as_of=as_of).n[0]) == 60
    assert int(c.execute_query("SELECT COUNT(*) AS n FROM sales.orders").n[0]) == 90


def test_iceberg_refuses_scans_over_the_limit_but_prunes_partitions(iceberg_catalog):
    from app.data_sources.clients.iceberg_client import IcebergScanTooLarge

    c = _ice(iceberg_catalog, max_scan_rows=20)
    with pytest.raises(IcebergScanTooLarge, match="order_ts"):
        c.execute_query("SELECT * FROM sales.orders")
    # One month is 15 rows: the date-string filter must prune to that partition.
    df = c.execute_query("SELECT COUNT(*) AS n FROM sales.orders WHERE order_ts >= '2025-03-01' AND order_ts < '2025-04-01'")
    assert int(df.n[0]) == 15


@pytest.mark.parametrize("bad", [
    "DELETE FROM sales.orders", "DROP TABLE sales.orders", "SELECT 1; DROP TABLE sales.orders",
    "INSERT INTO sales.orders VALUES (1, 'x', 1.0, NULL)",
])
def test_iceberg_refuses_writes(iceberg_catalog, bad):
    with pytest.raises(ReadOnlyViolation):
        _ice(iceberg_catalog).execute_query(bad)


def test_iceberg_unknown_table_lists_the_real_ones(iceberg_catalog):
    with pytest.raises(ValueError, match="sales.orders"):
        _ice(iceberg_catalog).execute_query("SELECT * FROM nope.nothing")


@pytest.mark.parametrize("where,expected", [
    ("order_ts >= '2025-03-01'", "GreaterThanOrEqual"),
    ("'2025-03-01' <= order_ts", "GreaterThanOrEqual"),  # flipped comparison
    ("region IN ('EMEA', 'APAC')", "In"),
    ("amount BETWEEN 1 AND 5", "And"),
    ("region = 'EMEA' AND amount > 3", "And"),
])
def test_pushdown_builds_iceberg_predicates(iceberg_catalog, where, expected):
    import sqlglot

    from app.data_sources.clients.iceberg_client import _pushdown_filter

    tree = sqlglot.parse_one(f"SELECT * FROM sales.orders WHERE {where}", read="duckdb")
    f = _pushdown_filter(tree, iceberg_catalog["orders"].schema().fields)
    assert type(f).__name__ == expected
    if "order_ts" in where:
        assert "2025-03-01T00:00:00" in repr(f)  # date string coerced to an ISO timestamp


@pytest.mark.parametrize("where", ["region = 'EMEA' OR amount > 3", "NOT region = 'EMEA'", "upper(region) = 'EMEA'"])
def test_pushdown_skips_what_it_cannot_prove(iceberg_catalog, where):
    import sqlglot

    from app.data_sources.clients.iceberg_client import _pushdown_filter

    tree = sqlglot.parse_one(f"SELECT * FROM sales.orders WHERE {where}", read="duckdb")
    assert _pushdown_filter(tree, iceberg_catalog["orders"].schema().fields) is None


@pytest.mark.parametrize("kw,expect", [
    (dict(catalog_type="rest", uri="https://cat/api", token="t"), {"type": "rest", "token": "t",
                                                                    "header.X-Iceberg-Access-Delegation": "vended-credentials"}),
    (dict(catalog_type="rest", uri="https://cat/api", client_id="i", client_secret="s", scope="PRINCIPAL_ROLE:ALL",
          vended_credentials=False), {"credential": "i:s", "scope": "PRINCIPAL_ROLE:ALL"}),
    (dict(catalog_type="glue", region="eu-west-1", access_key="A", secret_key="S"),
     {"type": "glue", "client.region": "eu-west-1", "client.access-key-id": "A"}),
    (dict(catalog_type="s3tables", region="us-east-1", warehouse="arn:aws:s3tables:us-east-1:1:bucket/b"),
     {"uri": "https://s3tables.us-east-1.amazonaws.com/iceberg", "rest.sigv4-enabled": "true",
      "rest.signing-name": "s3tables", "rest.signing-region": "us-east-1"}),
])
def test_iceberg_catalog_properties(kw, expect):
    from app.data_sources.clients.iceberg_client import IcebergClient

    props = IcebergClient(**kw)._properties()
    for k, v in expect.items():
        assert props[k] == v
    if kw.get("vended_credentials") is False:
        assert "header.X-Iceberg-Access-Delegation" not in props


@pytest.mark.parametrize("kw", [
    dict(catalog_type="rest"),  # no URI
    dict(catalog_type="s3tables", region="us-east-1"),  # no table bucket ARN
    dict(catalog_type="hive", uri="thrift://x"),
    dict(catalog_type="rest", uri="https://c", extra_properties="[1, 2]"),
])
def test_iceberg_misconfiguration_fails_the_connection_test_clearly(kw):
    from app.data_sources.clients.iceberg_client import IcebergClient

    res = IcebergClient(**kw).test_connection()
    assert res["success"] is False and res["message"]


# ================================================================= Dremio


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.content = json.dumps(body).encode()
        self.text = json.dumps(body)

    def json(self):
        return self._body


class _FakeDremio:
    """The REST surface the client uses: login, submit, job status, paged results."""

    def __init__(self, total=1234, state="COMPLETED", error=None):
        self.total, self.state, self.error = total, state, error
        self.calls, self.cancelled = [], False

    def __call__(self, session, method, url, **kw):
        self.calls.append((method, url.split("9047", 1)[-1], kw.get("params"), dict(session.headers)))
        if url.endswith("/apiv2/login"):
            ok = kw["json"]["password"] == "pw"
            return _Resp(200 if ok else 401, {"token": "tok"} if ok else {"errorMessage": "bad login"})
        if url.endswith("/sql"):
            return _Resp(200, {"id": "job1"})
        if url.endswith("/job/job1"):
            return _Resp(200, {"jobState": self.state, "rowCount": self.total, "errorMessage": self.error})
        if url.endswith("/cancel"):
            self.cancelled = True
            return _Resp(204, {})
        if url.endswith("/results"):
            off, lim = kw["params"]["offset"], kw["params"]["limit"]
            n = max(0, min(lim, self.total - off))
            return _Resp(200, {"rowCount": self.total, "schema": [{"name": "i"}],
                               "rows": [{"i": off + k} for k in range(n)]})
        return _Resp(404, {"errorMessage": url})


@pytest.fixture
def dremio(monkeypatch):
    import requests

    fake = _FakeDremio()
    monkeypatch.setattr(requests.Session, "request", lambda self, method, url, **kw: fake(self, method, url, **kw))
    monkeypatch.setattr(requests.Session, "post", lambda self, url, **kw: fake(self, "POST", url, **kw))
    return fake


def _dremio(**kw):
    from app.data_sources.clients.dremio_client import DremioClient

    return DremioClient(url="http://dremio:9047", **{"token": "pat", **kw})


def test_dremio_pages_results_up_to_the_cap(dremio):
    df = _dremio(max_rows=1100).execute_query("SELECT i FROM t")
    assert list(df.i) == list(range(1100))
    assert df.attrs["truncated"] is True
    pages = [c for c in dremio.calls if c[1].endswith("/results")]
    assert all(p[2]["limit"] <= 500 for p in pages)  # the API's page maximum


def test_dremio_full_result_is_not_truncated(dremio):
    dremio.total = 42
    df = _dremio().execute_query("SELECT i FROM t")
    assert len(df) == 42 and df.attrs["truncated"] is False


def test_dremio_failed_job_surfaces_the_engine_error(dremio):
    dremio.state, dremio.error = "FAILED", "Table 'x' not found"
    with pytest.raises(ValueError, match="not found"):
        _dremio().execute_query("SELECT * FROM x")


def test_dremio_auth_headers(dremio):
    _dremio().execute_query("SELECT 1")
    assert dremio.calls[0][3]["Authorization"] == "Bearer pat"
    dremio.calls.clear()
    _dremio(token=None, username="u", password="pw").execute_query("SELECT 1")
    assert any(c[3].get("Authorization") == "_dremiotok" for c in dremio.calls if c[1].endswith("/sql"))
    assert _dremio(token=None, username="u", password="wrong").test_connection()["success"] is False


def test_dremio_cloud_uses_the_project_api(dremio):
    _dremio(project_id="p1").execute_query("SELECT 1")
    assert any("/v0/projects/p1/sql" in c[1] for c in dremio.calls)


def test_dremio_refuses_writes_without_calling_the_server(dremio):
    for bad in ['DROP VIEW "s"."v"', 'CREATE TABLE "$scratch".t AS SELECT 1', "SELECT 1; DROP TABLE t"]:
        with pytest.raises(ReadOnlyViolation):
            _dremio().execute_query(bad)
    assert not dremio.calls


# =================================================================== Hive


class _Cursor:
    def __init__(self, results):
        self.results, self.description, self._rows = results, None, []

    def execute(self, sql):
        rows, cols = self.results[sql.split(" `")[0] if sql.startswith("DESCRIBE") else sql]
        self._rows, self.description = list(rows), [(c,) for c in cols]

    def fetchmany(self, n):
        out, self._rows = self._rows[:n], self._rows[n:]
        return out

    def close(self):
        pass


def _hive(results, **kw):
    from app.data_sources.clients.hive_client import HiveClient

    c = HiveClient(host="h", **kw)
    c._conn = type("C", (), {"cursor": lambda self: _Cursor(results), "close": lambda self: None})()
    return c


DESCRIBE_PARTITIONED = ([
    ("order_id", "int", ""), ("region", "string", "Sales region"), ("order_month", "string", ""),
    ("", None, None), ("# Partition Information", None, None), ("# col_name", "data_type", "comment"),
    ("order_month", "string", ""),
], ["col_name", "data_type", "comment"])


def test_hive_catalog_flags_partition_columns():
    c = _hive({
        "SHOW DATABASES": ([("default",), ("sales",), ("sys",)], ["database_name"]),
        "SHOW TABLES IN `default`": ([], ["tab_name"]),
        "SHOW TABLES IN `sales`": ([("orders",)], ["tab_name"]),
        "DESCRIBE": DESCRIBE_PARTITIONED,
    })
    tables = c.get_schemas()
    assert [t.name for t in tables] == ["sales.orders"]
    cols = {col.name: col.description for col in tables[0].columns}
    assert list(cols) == ["order_id", "region", "order_month"]  # partition col listed once
    assert "partition column" in cols["order_month"]
    assert cols["region"] == "Sales region"
    assert tables[0].description == "Partitioned by order_month."


def test_hive_spark_show_tables_shape_and_temp_views():
    c = _hive({
        "SHOW DATABASES": ([("sales",)], ["namespace"]),
        "SHOW TABLES IN `sales`": ([("sales", "orders", False), ("", "tmp_view", True)], ["namespace", "tableName", "isTemporary"]),
        "DESCRIBE": ([("id", "bigint", None)], ["col_name", "data_type", "comment"]),
    }, engine="spark")
    assert [t.name for t in c.get_schemas()] == ["sales.orders"]


def test_hive_results_are_capped():
    c = _hive({"SELECT x FROM t": ([(i,) for i in range(50)], ["t.x"])}, max_rows=20)
    df = c.execute_query("SELECT x FROM t")
    assert len(df) == 20 and df.attrs["truncated"] is True
    assert list(df.columns) == ["x"]  # Hive's table-qualified names are stripped


@pytest.mark.parametrize("engine", ["hive", "impala", "spark"])
def test_hive_refuses_writes_for_every_engine(engine):
    c = _hive({}, engine=engine)
    for bad in ["DROP TABLE t", "INSERT INTO t VALUES (1)", "SELECT 1; DROP TABLE t", "TRUNCATE TABLE t"]:
        with pytest.raises(ReadOnlyViolation):
            c.execute_query(bad)


# ============================================================ cross-cutting


@pytest.mark.parametrize("type_name,dialect", [("iceberg", "duckdb"), ("dremio", "dremio"), ("hive", "hive")])
def test_sandbox_sql_guard_covers_the_client(type_name, dialect):
    from app.schemas.data_source_registry import REGISTRY, resolve_client_class

    entry = REGISTRY[type_name]
    base = {f: (fi.default if fi.default is not None and not fi.is_required() else "x")
            for f, fi in entry.config_schema.model_fields.items()}
    client = resolve_client_class(type_name)(**base)
    assert dialect_for_client(client) == dialect
    for variant in entry.credentials_auth.by_auth.values():
        resolve_client_class(type_name)(**base, **dict.fromkeys(variant.schema.model_fields, "x"))
    assert "read-only" in client.description.lower()
