"""Unit tests for DruidClient.

Covers:
- pydruid.db.connect kwargs construction (scheme / auth)
- comma-separated schema parsing (dedup, order)
- execute_query -> pandas.DataFrame with column names
- get_tables() mapping from INFORMATION_SCHEMA.COLUMNS (fqn, system-schema
  exclusion, explicit schema filter)
- test_connection() success / failure
- get_schema() obsolete

pydruid is mocked via sys.modules, so these run without a real Druid cluster
or the pydruid dependency installed.
"""
from __future__ import annotations

import sys
import types

import pandas as pd
import pytest

from app.data_sources.clients.druid_client import DruidClient


# ---------- fake pydruid plumbing ---------- #


class _FakeCursor:
    def __init__(self, rows, description):
        self._rows = rows
        self.description = description
        self.executed = []  # list of (sql, params)

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return self

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _FakeConnection:
    def __init__(self, cursor, on_connect=None):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


def _install_fake_pydruid(monkeypatch, cursor, capture=None, raise_on_connect=None):
    """Inject a fake `pydruid.db` whose connect(**kwargs) records kwargs and
    returns a connection yielding `cursor`."""

    def _connect(**kwargs):
        if capture is not None:
            capture["kwargs"] = kwargs
        if raise_on_connect is not None:
            raise raise_on_connect
        return _FakeConnection(cursor)

    pydruid = types.ModuleType("pydruid")
    pydruid_db = types.ModuleType("pydruid.db")
    pydruid_db.connect = _connect
    pydruid.db = pydruid_db
    monkeypatch.setitem(sys.modules, "pydruid", pydruid)
    monkeypatch.setitem(sys.modules, "pydruid.db", pydruid_db)


# ---------- connect kwargs / parsing ---------- #


class TestConnectKwargs:
    def test_defaults_http_no_auth(self):
        c = DruidClient(host="broker.example")
        kw = c._connect_kwargs
        assert kw["host"] == "broker.example"
        assert kw["port"] == 8082
        assert kw["path"] == "/druid/v2/sql/"
        assert kw["scheme"] == "http"
        assert "user" not in kw and "password" not in kw

    def test_secure_and_auth(self):
        c = DruidClient(host="h", port=8888, user="u", password="p", secure=True)
        kw = c._connect_kwargs
        assert kw["scheme"] == "https"
        assert kw["user"] == "u" and kw["password"] == "p"
        assert kw["port"] == 8888


class TestSchemaParsing:
    def test_comma_separated_dedup_and_order(self):
        c = DruidClient(host="h", schema=" druid , staging ,druid, marts ")
        assert c._schemas == ["druid", "staging", "marts"]

    def test_empty(self):
        assert DruidClient(host="h")._schemas == []
        assert DruidClient(host="h", schema="   ")._schemas == []


# ---------- query / schema / connection ---------- #


class TestQuery:
    def test_execute_query_returns_dataframe(self, monkeypatch):
        cursor = _FakeCursor(
            rows=[("2026-06-16", 5), ("2026-06-15", 3)],
            description=[("day",), ("cnt",)],
        )
        _install_fake_pydruid(monkeypatch, cursor)
        c = DruidClient(host="h")
        df = c.execute_query("SELECT day, cnt FROM druid.events")
        assert isinstance(df, pd.DataFrame)
        assert list(df.columns) == ["day", "cnt"]
        assert list(df["cnt"]) == [5, 3]
        assert cursor.executed[0][0] == "SELECT day, cnt FROM druid.events"

    def test_execute_query_raises(self, monkeypatch):
        cursor = _FakeCursor(rows=[], description=[])
        _install_fake_pydruid(monkeypatch, cursor, raise_on_connect=RuntimeError("boom"))
        c = DruidClient(host="h")
        with pytest.raises(RuntimeError, match="boom"):
            c.execute_query("SELECT 1")


class TestGetTables:
    def test_maps_information_schema(self, monkeypatch):
        rows = [
            ("druid", "wikipedia", "__time", "TIMESTAMP"),
            ("druid", "wikipedia", "page", "VARCHAR"),
            ("druid", "wikipedia", "added", "BIGINT"),
            ("druid", "metrics", "__time", "TIMESTAMP"),
            ("druid", "metrics", "value", "DOUBLE"),
        ]
        cursor = _FakeCursor(rows=rows, description=None)
        _install_fake_pydruid(monkeypatch, cursor)
        c = DruidClient(host="h")
        tables = c.get_tables()

        by_name = {t.name: t for t in tables}
        assert set(by_name) == {"druid.wikipedia", "druid.metrics"}
        wiki = by_name["druid.wikipedia"]
        assert [col.name for col in wiki.columns] == ["__time", "page", "added"]
        assert wiki.columns[0].dtype == "TIMESTAMP"
        assert wiki.metadata_json == {"schema": "druid"}

    def test_excludes_system_schemas_by_default(self, monkeypatch):
        cursor = _FakeCursor(rows=[], description=None)
        _install_fake_pydruid(monkeypatch, cursor)
        c = DruidClient(host="h")
        c.get_tables()
        sql, _ = cursor.executed[0]
        assert "TABLE_SCHEMA NOT IN" in sql
        # System schemas inlined as escaped string literals (pydruid has no
        # positional param support).
        assert "'INFORMATION_SCHEMA'" in sql and "'sys'" in sql

    def test_explicit_schema_filter(self, monkeypatch):
        cursor = _FakeCursor(rows=[], description=None)
        _install_fake_pydruid(monkeypatch, cursor)
        c = DruidClient(host="h", schema="druid, staging")
        c.get_tables()
        sql, _ = cursor.executed[0]
        assert "TABLE_SCHEMA IN" in sql
        assert "'druid'" in sql and "'staging'" in sql

    def test_returns_empty_on_error(self, monkeypatch):
        cursor = _FakeCursor(rows=[], description=None)
        _install_fake_pydruid(monkeypatch, cursor, raise_on_connect=RuntimeError("down"))
        c = DruidClient(host="h")
        assert c.get_tables() == []


class TestConnectionAndMisc:
    def test_test_connection_success(self, monkeypatch):
        cursor = _FakeCursor(rows=[(1,)], description=[("EXPR$0",)])
        _install_fake_pydruid(monkeypatch, cursor)
        res = DruidClient(host="h").test_connection()
        assert res["success"] is True

    def test_test_connection_failure(self, monkeypatch):
        cursor = _FakeCursor(rows=[], description=[])
        _install_fake_pydruid(monkeypatch, cursor, raise_on_connect=RuntimeError("refused"))
        res = DruidClient(host="h").test_connection()
        assert res["success"] is False and "refused" in res["message"]

    def test_get_schema_obsolete(self):
        with pytest.raises(NotImplementedError):
            DruidClient(host="h").get_schema("t")

    def test_prompt_schema_renders(self, monkeypatch):
        rows = [("druid", "events", "page", "VARCHAR")]
        cursor = _FakeCursor(rows=rows, description=None)
        _install_fake_pydruid(monkeypatch, cursor)
        out = DruidClient(host="h").prompt_schema()
        assert "druid.events" in out and "page" in out

    def test_description_contains_endpoint(self):
        d = DruidClient(host="h", port=8888, secure=True).description
        assert "https://h:8888/druid/v2/sql/" in d


class TestRegistryWiring:
    def test_registered_with_druid_config(self):
        from app.schemas.data_source_registry import REGISTRY, get_entry
        from app.schemas.data_sources.configs import DruidConfig

        entry = get_entry("druid")
        assert entry is REGISTRY["druid"]
        assert entry.config_schema is DruidConfig
        assert "userpass" in entry.credentials_auth.by_auth

    def test_dynamic_resolution_returns_druid_client(self):
        from app.schemas.data_source_registry import resolve_client_class

        assert resolve_client_class("druid") is DruidClient

    def test_config_validates_and_defaults(self):
        from app.schemas.data_sources.configs import DruidConfig

        cfg = DruidConfig(host="broker")
        assert cfg.port == 8082
        assert cfg.path == "/druid/v2/sql/"
        assert cfg.secure is False
