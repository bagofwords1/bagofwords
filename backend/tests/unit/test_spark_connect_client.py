"""Unit tests for SparkConnectClient.

Covers:
- sc:// remote URL construction (no-auth / token / ssl combinations)
- comma-separated database parsing
- execute_query -> toPandas() routing
- get_tables() mapping from the Spark catalog API
- test_connection() success/failure
- connect() prefers create() and always stops the session

pyspark is mocked, so these run without a real Spark cluster or the pyspark
dependency installed.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.data_sources.clients.spark_connect_client import (
    SparkConnectClient,
    SparkQueryGuardError,
    _parse_size_to_bytes,
    _max_scan_bytes_from_plan,
    _has_unfiltered_partition_scan,
)


# Representative EXPLAIN COST fragments (shapes emitted by Spark 3.5).
PLAN_UNFILTERED = (
    "== Optimized Logical Plan ==\n"
    "Aggregate ..., Statistics(sizeInBytes=12.0 GiB)\n"
    "+- Relation default.sales[id#1,amount#2,dt#3] parquet\n"
    "== Physical Plan ==\n"
    "FileScan parquet default.sales[id#1,amount#2,dt#3] "
    "Batched: true, PartitionFilters: [], PushedFilters: [], ..."
)
PLAN_FILTERED = (
    "== Physical Plan ==\n"
    "FileScan parquet default.sales[id#1,amount#2,dt#3] "
    "Batched: true, PartitionFilters: [isnotnull(dt#3), (dt#3 = 2026-06-01)], ..."
)
PLAN_BIG = "Relation x, Statistics(sizeInBytes=3.0 TiB, rowCount=9.0E9)"
PLAN_UNKNOWN = "Relation y, Statistics(sizeInBytes=8.0 EiB)"  # Long.MaxValue default


# ---------- fake pyspark plumbing ---------- #


def _column(name, dtype="string", description=None):
    c = MagicMock()
    c.name = name
    c.dataType = dtype
    c.description = description
    return c


def _named(name, description=None):
    o = MagicMock()
    o.name = name
    o.description = description
    return o


def _make_fake_spark(tables=None, columns=None, databases=("default",)):
    """Build a fake SparkSession whose builder.remote(...).create() returns it."""
    spark = MagicMock()

    # SQL execution
    sql_result = MagicMock()
    sql_result.toPandas.return_value = pd.DataFrame({"x": [1]})
    sql_result.collect.return_value = [MagicMock()]
    spark.sql.return_value = sql_result

    # Catalog
    spark.catalog.listDatabases.return_value = [_named(d) for d in databases]
    spark.catalog.listTables.return_value = [_named(t) for t in (tables or [])]
    spark.catalog.listColumns.return_value = list(columns or [])
    spark.catalog.currentDatabase.return_value = databases[0]

    return spark, sql_result


def _install_fake_pyspark(monkeypatch, spark):
    """Inject a fake `pyspark.sql` module whose SparkSession.builder yields `spark`."""
    builder = MagicMock()
    builder.remote.return_value = builder
    builder.create.return_value = spark
    builder.getOrCreate.return_value = spark

    SparkSession = MagicMock()
    SparkSession.builder = builder

    pyspark = types.ModuleType("pyspark")
    pyspark_sql = types.ModuleType("pyspark.sql")
    pyspark_sql.SparkSession = SparkSession
    pyspark.sql = pyspark_sql
    monkeypatch.setitem(sys.modules, "pyspark", pyspark)
    monkeypatch.setitem(sys.modules, "pyspark.sql", pyspark_sql)
    return builder, SparkSession


# ---------- URL construction ---------- #


class TestRemoteUrl:
    def test_no_auth_default_port(self):
        c = SparkConnectClient(host="spark.example.ts.net")
        assert c.remote_url == "sc://spark.example.ts.net:15002/"

    def test_token_appended(self):
        c = SparkConnectClient(host="h", port=15003, token="abc")
        assert c.remote_url == "sc://h:15003/;token=abc"

    def test_ssl_and_token(self):
        c = SparkConnectClient(host="h", token="a/b c", use_ssl=True)
        # ssl first, token url-encoded
        assert c.remote_url == "sc://h:15002/;use_ssl=true;token=a%2Fb%20c"

    def test_ssl_as_string_truthy(self):
        c = SparkConnectClient(host="h", use_ssl="true")
        assert c.use_ssl is True
        assert "use_ssl=true" in c.remote_url


class TestDatabaseParsing:
    def test_comma_separated_dedup_and_order(self):
        c = SparkConnectClient(host="h", database=" a , b ,a, c ")
        assert c._databases == ["a", "b", "c"]

    def test_empty(self):
        c = SparkConnectClient(host="h")
        assert c._databases == []


# ---------- query / schema / connection ---------- #


class TestQueryAndSchema:
    def test_execute_query_returns_dataframe(self, monkeypatch):
        spark, sql_result = _make_fake_spark()
        _install_fake_pyspark(monkeypatch, spark)
        c = SparkConnectClient(host="h")
        df = c.execute_query("SELECT 1 AS x")
        spark.sql.assert_called_once_with("SELECT 1 AS x")
        sql_result.toPandas.assert_called_once()
        assert isinstance(df, pd.DataFrame) and list(df["x"]) == [1]
        spark.stop.assert_called_once()  # session always stopped

    def test_connect_prefers_create(self, monkeypatch):
        spark, _ = _make_fake_spark()
        builder, _ = _install_fake_pyspark(monkeypatch, spark)
        c = SparkConnectClient(host="h", catalog="main")
        with c.connect() as s:
            assert s is spark
        builder.remote.assert_called_once_with("sc://h:15002/")
        builder.create.assert_called_once()
        builder.getOrCreate.assert_not_called()
        spark.catalog.setCurrentCatalog.assert_called_once_with("main")

    def test_get_tables_maps_catalog(self, monkeypatch):
        cols = [_column("id", "bigint", "primary id"), _column("amount", "double")]
        spark, _ = _make_fake_spark(tables=["sales"], columns=cols, databases=("analytics",))
        _install_fake_pyspark(monkeypatch, spark)
        c = SparkConnectClient(host="h", database="analytics")
        tables = c.get_tables()
        assert len(tables) == 1
        t = tables[0]
        assert t.name == "analytics.sales"
        assert [col.name for col in t.columns] == ["id", "amount"]
        assert t.columns[0].dtype == "bigint"
        assert t.columns[0].description == "primary id"

    def test_test_connection_success(self, monkeypatch):
        spark, _ = _make_fake_spark()
        _install_fake_pyspark(monkeypatch, spark)
        c = SparkConnectClient(host="h")
        res = c.test_connection()
        assert res["success"] is True

    def test_test_connection_failure(self, monkeypatch):
        spark, _ = _make_fake_spark()
        spark.sql.side_effect = RuntimeError("boom")
        _install_fake_pyspark(monkeypatch, spark)
        c = SparkConnectClient(host="h")
        res = c.test_connection()
        assert res["success"] is False and "boom" in res["message"]

    def test_get_schema_not_implemented(self):
        c = SparkConnectClient(host="h")
        with pytest.raises(NotImplementedError):
            c.get_schema("t")


# ---------- EXPLAIN gate: pure parsers ---------- #


class TestPlanParsers:
    def test_parse_size_units(self):
        assert _parse_size_to_bytes("1", "B") == 1
        assert _parse_size_to_bytes("2", "KiB") == 2048
        assert _parse_size_to_bytes("1.5", "GiB") == int(1.5 * 1024 ** 3)

    def test_max_scan_bytes_picks_largest_known(self):
        assert _max_scan_bytes_from_plan(PLAN_BIG) == 3 * 1024 ** 4

    def test_max_scan_bytes_ignores_unknown_default(self):
        # 8 EiB is Spark's no-stats default -> treated as unknown -> None
        assert _max_scan_bytes_from_plan(PLAN_UNKNOWN) is None

    def test_max_scan_bytes_none_when_no_sizes(self):
        assert _max_scan_bytes_from_plan("no sizes here") is None

    def test_unfiltered_partition_scan_detection(self):
        assert _has_unfiltered_partition_scan(PLAN_UNFILTERED) is True
        assert _has_unfiltered_partition_scan(PLAN_FILTERED) is False


# ---------- EXPLAIN gate: end-to-end via execute_query ---------- #


def _install_plan_aware_spark(monkeypatch, plan_text):
    """Fake spark whose sql('EXPLAIN COST ...') returns plan_text, else data."""
    spark = MagicMock()

    def _sql(q):
        res = MagicMock()
        if q.strip().upper().startswith("EXPLAIN COST"):
            res.collect.return_value = [(plan_text,)]
        else:
            res.toPandas.return_value = pd.DataFrame({"x": [1]})
            res.collect.return_value = [MagicMock()]
        return res

    spark.sql.side_effect = _sql
    _install_fake_pyspark(monkeypatch, spark)
    return spark


class TestExplainGate:
    def test_partition_filter_required_rejects(self, monkeypatch):
        _install_plan_aware_spark(monkeypatch, PLAN_UNFILTERED)
        c = SparkConnectClient(host="h", require_partition_filter=True)
        with pytest.raises(SparkQueryGuardError, match="partition"):
            c.execute_query("SELECT * FROM sales")

    def test_partition_filter_required_allows_filtered(self, monkeypatch):
        _install_plan_aware_spark(monkeypatch, PLAN_FILTERED)
        c = SparkConnectClient(host="h", require_partition_filter=True)
        df = c.execute_query("SELECT * FROM sales WHERE dt = '2026-06-01'")
        assert list(df["x"]) == [1]

    def test_max_scan_bytes_rejects_over_limit(self, monkeypatch):
        _install_plan_aware_spark(monkeypatch, PLAN_BIG)
        c = SparkConnectClient(host="h", max_scan_bytes=1024 ** 3)  # 1 GiB cap
        with pytest.raises(SparkQueryGuardError, match="scan size"):
            c.execute_query("SELECT * FROM x")

    def test_max_scan_bytes_unknown_fails_open(self, monkeypatch):
        _install_plan_aware_spark(monkeypatch, PLAN_UNKNOWN)
        c = SparkConnectClient(host="h", max_scan_bytes=1024 ** 3)
        # no usable estimate -> do not block
        assert list(c.execute_query("SELECT * FROM y")["x"]) == [1]

    def test_guard_disabled_skips_explain(self, monkeypatch):
        spark = _install_plan_aware_spark(monkeypatch, PLAN_UNFILTERED)
        c = SparkConnectClient(host="h")  # no gate configured
        c.execute_query("SELECT * FROM sales")
        # only the real query ran; no EXPLAIN COST issued
        assert all(
            not call.args[0].upper().startswith("EXPLAIN")
            for call in spark.sql.call_args_list
        )

    def test_explain_error_fails_open(self, monkeypatch):
        spark = MagicMock()

        def _sql(q):
            if q.strip().upper().startswith("EXPLAIN COST"):
                raise RuntimeError("cannot explain")
            res = MagicMock()
            res.toPandas.return_value = pd.DataFrame({"x": [1]})
            return res

        spark.sql.side_effect = _sql
        _install_fake_pyspark(monkeypatch, spark)
        c = SparkConnectClient(host="h", require_partition_filter=True)
        # explain blew up -> query still runs
        assert list(c.execute_query("SELECT * FROM sales")["x"]) == [1]
