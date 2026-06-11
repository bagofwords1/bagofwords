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

from app.data_sources.clients.spark_connect_client import SparkConnectClient


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
