"""Iceberg (REST catalog), Dremio and Hive clients against real servers:
the Apache Iceberg REST fixture, Dremio OSS and HiveServer2. Needs Docker.

Each is seeded with the same 900 orders over three regions and six months
and held to one contract: the catalog lists the table, an aggregate matches
the seed, a filter on the partition / date column returns the right month,
results are capped and flagged, and writes are refused.

The AWS Glue and Amazon S3 Tables catalogs of the Iceberg client need real
AWS resources; their loop is in docs/feedback-loops/new-connectors.md.

Run:
    cd backend
    uv run pytest tests/integrations/test_lakehouse_live.py -v
"""
from __future__ import annotations

import datetime as dt
import os
import tempfile
import time

import pytest

tc_core = pytest.importorskip("testcontainers.core.container")

REGIONS = ["EMEA", "AMER", "APAC"]
N = 900
MARCH = sum(1 for i in range(N) if 1 + i % 6 == 3)
REVENUE = {r: round(sum(10 + i * 1.5 for i in range(N) if REGIONS[i % 3] == r), 2) for r in REGIONS}


# Containers started for the current backend; stopped on teardown even when
# seeding fails half-way (otherwise a failed setup leaks a running server).
_STARTED: list = []


def _stop_started() -> None:
    while _STARTED:
        try:
            _STARTED.pop().stop()
        except Exception:
            pass


def _start(container, what):
    try:
        container.start()
        _STARTED.append(container)
    except Exception as e:
        pytest.skip(f"Docker / {what} unavailable: {e}")
    return container


def _retry(fn, timeout=240, every=3):
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        try:
            return fn()
        except Exception as e:
            last = e
            time.sleep(every)
    raise TimeoutError(f"not ready after {timeout}s: {last}")


class Lake:
    def __init__(self, name, client, table, revenue_sql, march_sql, all_sql, writes):
        self.name, self.client, self.table = name, client, table
        self.revenue_sql, self.march_sql, self.all_sql, self.writes = revenue_sql, march_sql, all_sql, writes


def _iceberg_rest():
    import pyarrow as pa
    from pyiceberg.catalog import load_catalog
    from pyiceberg.partitioning import PartitionField, PartitionSpec
    from pyiceberg.schema import Schema
    from pyiceberg.transforms import MonthTransform
    from pyiceberg.types import DoubleType, LongType, NestedField, StringType, TimestampType

    from app.data_sources.clients.iceberg_client import IcebergClient

    # The fixture writes data files to its warehouse path; the client reads the
    # same files, so the directory is shared at the same path.
    wh = tempfile.mkdtemp(prefix="bow-iceberg-")
    os.chmod(wh, 0o777)
    c = (tc_core.DockerContainer("apache/iceberg-rest-fixture:1.9.1").with_exposed_ports(8181)
         .with_volume_mapping(wh, wh, "rw").with_env("CATALOG_WAREHOUSE", f"file://{wh}"))
    _start(c, "iceberg-rest-fixture")
    uri = f"http://{c.get_container_host_ip()}:{c.get_exposed_port(8181)}"
    cat = _retry(lambda: (load_catalog("seed", type="rest", uri=uri).list_namespaces(), load_catalog("seed", type="rest", uri=uri))[1])
    cat.create_namespace("sales")
    schema = Schema(NestedField(1, "order_id", LongType(), required=False), NestedField(2, "region", StringType(), required=False),
                    NestedField(3, "amount", DoubleType(), required=False), NestedField(4, "order_ts", TimestampType(), required=False))
    t = cat.create_table(("sales", "orders"), schema=schema, partition_spec=PartitionSpec(
        PartitionField(source_id=4, field_id=1000, transform=MonthTransform(), name="m")))
    t.append(pa.Table.from_pylist([
        {"order_id": i, "region": REGIONS[i % 3], "amount": 10 + i * 1.5, "order_ts": dt.datetime(2025, 1 + i % 6, 1 + i % 27)}
        for i in range(N)], schema=t.schema().as_arrow()))
    client = IcebergClient(catalog_type="rest", uri=uri, max_scan_rows=N)
    return c, Lake(
        "iceberg", client, "sales.orders",
        revenue_sql="SELECT region, ROUND(SUM(amount), 2) AS revenue FROM sales.orders GROUP BY region",
        march_sql="SELECT COUNT(*) AS n FROM sales.orders WHERE order_ts >= '2025-03-01' AND order_ts < '2025-04-01'",
        all_sql=None,  # Iceberg refuses oversized scans instead of truncating (covered in unit tests)
        writes=["DELETE FROM sales.orders", "DROP TABLE sales.orders"],
    )


def _dremio():
    import requests

    from app.data_sources.clients.dremio_client import DremioClient

    c = _start(tc_core.DockerContainer("dremio/dremio-oss:25.2").with_exposed_ports(9047), "dremio")
    base = f"http://{c.get_container_host_ip()}:{c.get_exposed_port(9047)}"
    s = requests.Session()
    _retry(lambda: s.get(base, timeout=5).raise_for_status(), timeout=300, every=5)
    s.put(f"{base}/apiv2/bootstrap/firstuser", headers={"Authorization": "_dremionull"}, json={
        "userName": "admin", "firstName": "a", "lastName": "b", "email": "a@b.co",
        "createdAt": int(time.time() * 1000), "password": "Passw0rd!x"}).raise_for_status()
    admin = DremioClient(url=base, username="admin", password="Passw0rd!x")
    admin._session().post(f"{base}/api/v3/catalog", json={"entityType": "space", "name": "Sales"}).raise_for_status()
    vals = ", ".join(f"({i}, '{REGIONS[i % 3]}', {10 + i * 1.5}, DATE '2025-0{1 + i % 6}-{1 + i % 27:02d}')" for i in range(N))
    admin._run(f'CREATE TABLE "$scratch".orders AS SELECT * FROM (VALUES {vals}) AS t(order_id, region, amount, order_date)', 1)
    admin._run('CREATE VIEW "Sales"."orders" AS SELECT * FROM "$scratch".orders', 1)
    client = DremioClient(url=base, username="admin", password="Passw0rd!x", max_rows=700)
    return c, Lake(
        "dremio", client, "Sales.orders",
        revenue_sql='SELECT region, ROUND(SUM(amount), 2) AS revenue FROM "Sales"."orders" GROUP BY region',
        march_sql="SELECT COUNT(*) AS n FROM \"Sales\".\"orders\" WHERE order_date >= DATE '2025-03-01' AND order_date < DATE '2025-04-01'",
        all_sql='SELECT * FROM "Sales"."orders"',
        writes=['DROP VIEW "Sales"."orders"', 'CREATE TABLE "$scratch".x AS SELECT 1 AS a'],
    )


def _hive():
    from pyhive import hive

    from app.data_sources.clients.hive_client import HiveClient

    c = _start(tc_core.DockerContainer("apache/hive:4.0.1").with_exposed_ports(10000).with_env("SERVICE_NAME", "hiveserver2"), "hive")
    host, port = c.get_container_host_ip(), int(c.get_exposed_port(10000))
    cur = _retry(lambda: hive.connect(host=host, port=port, username="hive", auth="NONE").cursor(), timeout=300, every=5)
    cur.execute("CREATE DATABASE sales")
    cur.execute("CREATE TABLE sales.orders (order_id INT, region STRING, amount DOUBLE) PARTITIONED BY (order_month STRING)")
    cur.execute("SET hive.exec.dynamic.partition.mode=nonstrict")
    vals = ", ".join(f"({i}, '{REGIONS[i % 3]}', {10 + i * 1.5}, '2025-0{1 + i % 6}')" for i in range(N))
    cur.execute(f"INSERT INTO sales.orders PARTITION (order_month) VALUES {vals}")
    client = HiveClient(host=host, port=port, max_rows=700)
    return c, Lake(
        "hive", client, "sales.orders",
        revenue_sql="SELECT region, ROUND(SUM(amount), 2) AS revenue FROM sales.orders GROUP BY region",
        march_sql="SELECT COUNT(*) AS n FROM sales.orders WHERE order_month = '2025-03'",
        all_sql="SELECT * FROM sales.orders",
        writes=["DROP TABLE sales.orders", "INSERT INTO sales.orders PARTITION (order_month='x') VALUES (1, 'a', 1.0)"],
    )


FACTORIES = {"iceberg": _iceberg_rest, "dremio": _dremio, "hive": _hive}


@pytest.fixture(scope="module", params=list(FACTORIES))
def lake(request):
    try:
        _container, lake = FACTORIES[request.param]()
        yield lake
    finally:
        _stop_started()


def test_connection_succeeds(lake):
    res = lake.client.test_connection()
    assert res["success"], res["message"]


def test_catalog_lists_the_orders_table(lake):
    tables = {t.name: t for t in lake.client.get_schemas()}
    assert lake.table in tables, sorted(tables)
    assert {"region", "amount"} <= {c.name.lower() for c in tables[lake.table].columns}


def test_aggregate_matches_the_seed(lake):
    df = lake.client.execute_query(lake.revenue_sql)
    assert {r: round(float(v), 2) for r, v in zip(df["region"], df["revenue"], strict=True)} == REVENUE


def test_date_or_partition_filter(lake):
    assert int(lake.client.execute_query(lake.march_sql)["n"].iloc[0]) == MARCH


def test_results_are_capped_and_flagged(lake):
    if not lake.all_sql:
        pytest.skip("this engine refuses oversized reads instead of truncating")
    df = lake.client.execute_query(lake.all_sql)
    assert len(df) == 700 and df.attrs["truncated"] is True


def test_writes_are_refused(lake):
    for stmt in lake.writes:
        with pytest.raises(ValueError):
            lake.client.execute_query(stmt)
    assert lake.table in {t.name for t in lake.client.get_schemas()}
