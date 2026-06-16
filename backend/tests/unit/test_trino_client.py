"""Unit tests for TrinoClient.

Covers connect-kwarg construction (catalog/schema/auth/scheme), schema
parsing, execute_query -> DataFrame, information_schema discovery (catalog-
qualified ref, FQN mapping, system-schema exclusion, explicit filter),
test_connection, and registry wiring.

The `trino` driver is mocked via sys.modules, so these run without a real
Trino cluster or the trino dependency installed.
"""
from __future__ import annotations

import sys
import types

import pandas as pd
import pytest

from app.data_sources.clients.trino_client import TrinoClient


class _FakeCursor:
    def __init__(self, rows, description):
        self._rows = rows
        self.description = description
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return self

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


def _install_fake_trino(monkeypatch, cursor, capture=None, raise_on_connect=None):
    def _connect(**kwargs):
        if capture is not None:
            capture["kwargs"] = kwargs
        if raise_on_connect is not None:
            raise raise_on_connect
        return _FakeConnection(cursor)

    class _BasicAuth:
        def __init__(self, user, password):
            self.user = user
            self.password = password

    trino = types.ModuleType("trino")
    dbapi = types.ModuleType("trino.dbapi")
    dbapi.connect = _connect
    auth = types.ModuleType("trino.auth")
    auth.BasicAuthentication = _BasicAuth
    trino.dbapi = dbapi
    trino.auth = auth
    monkeypatch.setitem(sys.modules, "trino", trino)
    monkeypatch.setitem(sys.modules, "trino.dbapi", dbapi)
    monkeypatch.setitem(sys.modules, "trino.auth", auth)


class TestConnectKwargs:
    def test_defaults(self, monkeypatch):
        cap = {}
        _install_fake_trino(monkeypatch, _FakeCursor([(1,)], [("c",)]), capture=cap)
        TrinoClient(host="h", catalog="tpch").execute_query("SELECT 1")
        kw = cap["kwargs"]
        assert kw["host"] == "h" and kw["port"] == 8080
        assert kw["catalog"] == "tpch" and kw["http_scheme"] == "http"
        assert kw["user"] == "trino"
        assert "auth" not in kw

    def test_password_forces_https_and_auth(self, monkeypatch):
        cap = {}
        _install_fake_trino(monkeypatch, _FakeCursor([(1,)], [("c",)]), capture=cap)
        c = TrinoClient(host="h", catalog="hive", user="bob", password="secret")
        assert c.http_scheme == "https"
        c.execute_query("SELECT 1")
        kw = cap["kwargs"]
        assert kw["http_scheme"] == "https"
        assert kw["auth"].user == "bob" and kw["auth"].password == "secret"

    def test_single_schema_set_on_session(self, monkeypatch):
        cap = {}
        _install_fake_trino(monkeypatch, _FakeCursor([(1,)], [("c",)]), capture=cap)
        TrinoClient(host="h", catalog="c", schema="sales").execute_query("SELECT 1")
        assert cap["kwargs"]["schema"] == "sales"

    def test_multi_schema_not_set_on_session(self, monkeypatch):
        cap = {}
        _install_fake_trino(monkeypatch, _FakeCursor([(1,)], [("c",)]), capture=cap)
        TrinoClient(host="h", catalog="c", schema="a,b").execute_query("SELECT 1")
        assert "schema" not in cap["kwargs"]


class TestSchemaParsing:
    def test_dedup_and_order(self):
        c = TrinoClient(host="h", catalog="c", schema=" a , b ,a, c ")
        assert c._schemas == ["a", "b", "c"]

    def test_empty(self):
        assert TrinoClient(host="h", catalog="c")._schemas == []


class TestQuery:
    def test_execute_query_dataframe(self, monkeypatch):
        cursor = _FakeCursor(rows=[("x", 10), ("y", 20)], description=[("k",), ("v",)])
        _install_fake_trino(monkeypatch, cursor)
        df = TrinoClient(host="h", catalog="c").execute_query("SELECT k, v FROM t")
        assert list(df.columns) == ["k", "v"]
        assert list(df["v"]) == [10, 20]

    def test_execute_query_raises(self, monkeypatch):
        _install_fake_trino(monkeypatch, _FakeCursor([], []), raise_on_connect=RuntimeError("boom"))
        with pytest.raises(RuntimeError, match="boom"):
            TrinoClient(host="h", catalog="c").execute_query("SELECT 1")


class TestGetTables:
    def test_maps_information_schema(self, monkeypatch):
        rows = [
            ("sales", "orders", "id", "bigint"),
            ("sales", "orders", "total", "double"),
            ("sales", "customers", "name", "varchar"),
        ]
        cursor = _FakeCursor(rows=rows, description=None)
        _install_fake_trino(monkeypatch, cursor)
        tables = TrinoClient(host="h", catalog="hive").get_tables()
        by = {t.name: t for t in tables}
        assert set(by) == {"sales.orders", "sales.customers"}
        assert [c.name for c in by["sales.orders"].columns] == ["id", "total"]
        assert by["sales.orders"].columns[0].dtype == "bigint"
        assert by["sales.orders"].metadata_json == {"schema": "sales", "catalog": "hive"}

    def test_catalog_qualified_info_schema(self, monkeypatch):
        cursor = _FakeCursor(rows=[], description=None)
        _install_fake_trino(monkeypatch, cursor)
        TrinoClient(host="h", catalog="my_cat").get_tables()
        sql, _ = cursor.executed[0]
        assert '"my_cat".information_schema.columns' in sql
        assert "table_schema NOT IN" in sql and "'information_schema'" in sql

    def test_explicit_schema_filter(self, monkeypatch):
        cursor = _FakeCursor(rows=[], description=None)
        _install_fake_trino(monkeypatch, cursor)
        TrinoClient(host="h", catalog="c", schema="sales, ops").get_tables()
        sql, _ = cursor.executed[0]
        assert "table_schema IN" in sql
        assert "'sales'" in sql and "'ops'" in sql

    def test_returns_empty_on_error(self, monkeypatch):
        _install_fake_trino(monkeypatch, _FakeCursor([], None), raise_on_connect=RuntimeError("down"))
        assert TrinoClient(host="h", catalog="c").get_tables() == []


class TestConnectionAndRegistry:
    def test_test_connection_success(self, monkeypatch):
        _install_fake_trino(monkeypatch, _FakeCursor([(1,)], [("x",)]))
        assert TrinoClient(host="h", catalog="c").test_connection()["success"] is True

    def test_test_connection_failure(self, monkeypatch):
        _install_fake_trino(monkeypatch, _FakeCursor([], []), raise_on_connect=RuntimeError("refused"))
        res = TrinoClient(host="h", catalog="c").test_connection()
        assert res["success"] is False and "refused" in res["message"]

    def test_get_schema_obsolete(self):
        with pytest.raises(NotImplementedError):
            TrinoClient(host="h", catalog="c").get_schema("t")

    def test_description(self):
        d = TrinoClient(host="h", port=8443, catalog="iceberg", password="p").description
        assert "https://h:8443" in d and "catalog=iceberg" in d

    def test_registry_wiring(self):
        from app.schemas.data_source_registry import get_entry, resolve_client_class
        from app.schemas.data_sources.configs import TrinoConfig

        entry = get_entry("trino")
        assert entry.config_schema is TrinoConfig
        assert resolve_client_class("trino") is TrinoClient

    def test_config_defaults(self):
        from app.schemas.data_sources.configs import TrinoConfig

        cfg = TrinoConfig(host="h", catalog="tpch")
        assert cfg.port == 8080 and cfg.http_scheme == "http"
