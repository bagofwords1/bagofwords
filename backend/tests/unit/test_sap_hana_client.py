"""Unit tests for SapHanaClient.

Covers:
- hdbcli.dbapi.connect kwargs construction (encrypt / TLS / tenant / currentSchema)
- comma-separated schema parsing (dedup, order)
- execute_query -> pandas.DataFrame with column names
- get_tables() mapping from SYS.TABLE_COLUMNS / SYS.VIEW_COLUMNS (fqn,
  table+view union, system-schema exclusion, explicit schema filter, PKs)
- test_connection() success / failure
- get_schema() obsolete
- registry wiring

hdbcli is mocked via sys.modules, so these run without a real HANA instance
or the hdbcli dependency installed.
"""
from __future__ import annotations

import sys
import types

import pandas as pd
import pytest

from app.data_sources.clients.sap_hana_client import SapHanaClient


# ---------- fake hdbcli plumbing ---------- #


class _FakeCursor:
    def __init__(self, results):
        # results: list of (rows, description) consumed per execute() call
        self._results = list(results)
        self._rows = []
        self.description = None
        self.executed = []  # list of (sql, params)

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self._results:
            self._rows, self.description = self._results.pop(0)
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


def _install_fake_hdbcli(monkeypatch, cursor, capture=None, raise_on_connect=None):
    """Inject a fake `hdbcli.dbapi` whose connect(**kwargs) records kwargs and
    returns a connection yielding `cursor`."""

    def _connect(**kwargs):
        if capture is not None:
            capture["kwargs"] = kwargs
        if raise_on_connect is not None:
            raise raise_on_connect
        return _FakeConnection(cursor)

    hdbcli = types.ModuleType("hdbcli")
    hdbcli_dbapi = types.ModuleType("hdbcli.dbapi")
    hdbcli_dbapi.connect = _connect
    hdbcli.dbapi = hdbcli_dbapi
    monkeypatch.setitem(sys.modules, "hdbcli", hdbcli)
    monkeypatch.setitem(sys.modules, "hdbcli.dbapi", hdbcli_dbapi)


def _single_result(rows, description=None):
    return _FakeCursor([(rows, description)])


# ---------- connect kwargs / parsing ---------- #


class TestConnectKwargs:
    def test_defaults_cloud_tls(self):
        c = SapHanaClient(host="x.hanacloud.ondemand.com", user="u", password="p")
        kw = c._connect_kwargs
        assert kw["address"] == "x.hanacloud.ondemand.com"
        assert kw["port"] == 443
        assert kw["encrypt"] is True
        assert kw["sslValidateCertificate"] is True
        assert "databaseName" not in kw and "currentSchema" not in kw

    def test_plain_onprem_no_tls(self):
        c = SapHanaClient(host="hxe", port=39041, user="u", password="p", encrypt=False)
        kw = c._connect_kwargs
        assert kw["port"] == 39041
        assert kw["encrypt"] is False
        # No TLS -> certificate validation must not be requested.
        assert kw["sslValidateCertificate"] is False

    def test_encrypt_without_verify(self):
        c = SapHanaClient(host="h", user="u", password="p", encrypt=True, verify_ssl=False)
        kw = c._connect_kwargs
        assert kw["encrypt"] is True and kw["sslValidateCertificate"] is False

    def test_tenant_database_and_current_schema(self):
        c = SapHanaClient(host="h", port=39013, user="u", password="p",
                          database="HXE", schema="SALES, MARTS")
        kw = c._connect_kwargs
        assert kw["databaseName"] == "HXE"
        assert kw["currentSchema"] == "SALES"


class TestSchemaParsing:
    def test_comma_separated_dedup_and_order(self):
        c = SapHanaClient(host="h", user="u", password="p", schema=" SALES , MARTS ,SALES, STAGE ")
        assert c._schemas == ["SALES", "MARTS", "STAGE"]

    def test_case_preserved_for_datasphere_spaces(self):
        # Datasphere space schemas can be case-sensitive; never upper-fold.
        c = SapHanaClient(host="h", user="u", password="p", schema="MySpace")
        assert c._schemas == ["MySpace"]

    def test_empty(self):
        assert SapHanaClient(host="h", user="u", password="p")._schemas == []
        assert SapHanaClient(host="h", user="u", password="p", schema="  ")._schemas == []


# ---------- query / schema / connection ---------- #


class TestQuery:
    def test_execute_query_returns_dataframe(self, monkeypatch):
        cursor = _single_result(
            rows=[("EMEA", 42), ("APJ", 7)],
            description=[("REGION",), ("CNT",)],
        )
        _install_fake_hdbcli(monkeypatch, cursor)
        c = SapHanaClient(host="h", user="u", password="p")
        df = c.execute_query('SELECT REGION, CNT FROM "SALES"."V_ORDERS"')
        assert isinstance(df, pd.DataFrame)
        assert list(df.columns) == ["REGION", "CNT"]
        assert list(df["CNT"]) == [42, 7]
        assert cursor.executed[0][0] == 'SELECT REGION, CNT FROM "SALES"."V_ORDERS"'

    def test_execute_query_raises(self, monkeypatch):
        cursor = _single_result(rows=[], description=[])
        _install_fake_hdbcli(monkeypatch, cursor, raise_on_connect=RuntimeError("boom"))
        c = SapHanaClient(host="h", user="u", password="p")
        with pytest.raises(RuntimeError, match="boom"):
            c.execute_query("SELECT 1 FROM DUMMY")


class TestGetTables:
    # Row shape: schema, object, column, dtype, col_comment, obj_comment, obj_type, position
    ROWS = [
        ("SALES", "ORDERS", "ID", "INTEGER", None, "All orders", "TABLE", 1),
        ("SALES", "ORDERS", "REGION", "NVARCHAR", "Sales region", "All orders", "TABLE", 2),
        ("SALES", "V_REVENUE", "REGION", "NVARCHAR", None, None, "VIEW", 1),
        ("SALES", "V_REVENUE", "REVENUE", "DECIMAL", None, None, "VIEW", 2),
    ]

    def test_maps_tables_and_views(self, monkeypatch):
        # First execute: PK query; second: enriched union query.
        cursor = _FakeCursor([
            ([("SALES", "ORDERS", "ID")], None),
            (self.ROWS, None),
        ])
        _install_fake_hdbcli(monkeypatch, cursor)
        c = SapHanaClient(host="h", user="u", password="p")
        tables = c.get_tables()

        by_name = {t.name: t for t in tables}
        assert set(by_name) == {"SALES.ORDERS", "SALES.V_REVENUE"}
        orders = by_name["SALES.ORDERS"]
        assert [col.name for col in orders.columns] == ["ID", "REGION"]
        assert orders.columns[1].description == "Sales region"
        assert orders.description == "All orders"
        assert [pk.name for pk in orders.pks] == ["ID"]
        assert orders.metadata_json == {"schema": "SALES", "object_type": "TABLE"}
        view = by_name["SALES.V_REVENUE"]
        assert view.metadata_json["object_type"] == "VIEW"
        assert view.pks == []

    def test_excludes_system_schemas_by_default(self, monkeypatch):
        cursor = _FakeCursor([([], None), ([], None)])
        _install_fake_hdbcli(monkeypatch, cursor)
        c = SapHanaClient(host="h", user="u", password="p")
        c.get_tables()
        sql, params = cursor.executed[-1]
        assert "NOT IN" in sql and "NOT LIKE" in sql
        assert "SYS" in params
        assert any(p.startswith("\\_SYS") for p in params)

    def test_explicit_schema_filter(self, monkeypatch):
        cursor = _FakeCursor([([], None), ([], None)])
        _install_fake_hdbcli(monkeypatch, cursor)
        c = SapHanaClient(host="h", user="u", password="p", schema="SALES, MARTS")
        c.get_tables()
        sql, params = cursor.executed[-1]
        assert "IN (?, ?)" in sql
        # Table branch + view branch both carry the schema list.
        assert params == ["SALES", "MARTS", "SALES", "MARTS"]

    def test_falls_back_to_basic_and_empty_on_error(self, monkeypatch):
        cursor = _single_result(rows=[], description=None)
        _install_fake_hdbcli(monkeypatch, cursor, raise_on_connect=RuntimeError("down"))
        c = SapHanaClient(host="h", user="u", password="p")
        assert c.get_tables() == []


class TestConnectionAndMisc:
    def test_test_connection_success(self, monkeypatch):
        cursor = _single_result(rows=[(1,)], description=[("1",)])
        _install_fake_hdbcli(monkeypatch, cursor)
        res = SapHanaClient(host="h", user="u", password="p").test_connection()
        assert res["success"] is True
        assert cursor.executed[0][0] == "SELECT 1 FROM DUMMY"

    def test_test_connection_failure(self, monkeypatch):
        cursor = _single_result(rows=[], description=[])
        _install_fake_hdbcli(monkeypatch, cursor, raise_on_connect=RuntimeError("refused"))
        res = SapHanaClient(host="h", user="u", password="p").test_connection()
        assert res["success"] is False and "refused" in res["message"]

    def test_get_schema_obsolete(self):
        with pytest.raises(NotImplementedError):
            SapHanaClient(host="h", user="u", password="p").get_schema("t")

    def test_prompt_schema_renders(self, monkeypatch):
        rows = [("SALES", "ORDERS", "REGION", "NVARCHAR", None, None, "TABLE", 1)]
        cursor = _FakeCursor([([], None), (rows, None)])
        _install_fake_hdbcli(monkeypatch, cursor)
        out = SapHanaClient(host="h", user="u", password="p").prompt_schema()
        assert "SALES.ORDERS" in out and "REGION" in out

    def test_description_mentions_target_and_dialect(self):
        c = SapHanaClient(host="hxe.local", port=39041, user="u", password="p", database="HXE")
        d = c.description
        assert "hxe.local:39041" in d and "HXE" in d
        assert "LIMIT" in d and "Datasphere" in d


class TestRegistryWiring:
    def test_registered_with_sap_hana_config(self):
        from app.schemas.data_source_registry import REGISTRY, get_entry
        from app.schemas.data_sources.configs import SapHanaConfig

        entry = get_entry("sap_hana")
        assert entry is REGISTRY["sap_hana"]
        assert entry.config_schema is SapHanaConfig
        assert "userpass" in entry.credentials_auth.by_auth

    def test_credentials_schema_resolves(self):
        from app.schemas.data_source_registry import credentials_schema_for
        from app.schemas.data_sources.configs import SapHanaCredentials

        assert credentials_schema_for("sap_hana", "userpass") is SapHanaCredentials

    def test_explicit_client_path_resolution(self):
        from app.schemas.data_source_registry import resolve_client_class

        assert resolve_client_class("sap_hana") is SapHanaClient

    def test_config_validates_and_defaults(self):
        from app.schemas.data_sources.configs import SapHanaConfig

        cfg = SapHanaConfig(host="x.hanacloud.ondemand.com")
        assert cfg.port == 443
        assert cfg.encrypt is True
        assert cfg.verify_ssl is True
        assert cfg.database is None

    def test_ctor_kwargs_match_config_plus_credentials(self):
        # The service merges config+credential fields into ctor kwargs — the
        # names must line up exactly or client construction breaks at runtime.
        import inspect
        from app.schemas.data_sources.configs import SapHanaConfig, SapHanaCredentials

        params = set(inspect.signature(SapHanaClient.__init__).parameters) - {"self"}
        fields = set(SapHanaConfig.model_fields) | set(SapHanaCredentials.model_fields)
        assert fields <= params
