"""Unit tests for TrinoClient (Trino, Starburst Galaxy, Starburst Enterprise).

Covers the connection contract that the live Starburst cluster exposed as
broken or missing (see docs/feedback-loops/starburst-connector.md):

- TLS reaches the driver explicitly (not inferred from the port), with
  certificate verification / CA bundle control
- credentials never travel over plain HTTP
- role, timezone, source and token auth
- discovery across the primary + additional catalogs, with comments, view
  flags and the naming convention the agent is told to use
- cheap table count for the connection test
- the cancellation hook the timeout path relies on

The driver boundary (`engine_pool.get_engine`) is faked; nothing here talks
to a cluster.
"""
from __future__ import annotations

from contextlib import contextmanager

import pandas as pd
import pytest

from app.data_sources.clients import trino_client as mod
from app.data_sources.clients.trino_client import TrinoClient


# ---------- fake driver plumbing ---------- #


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def scalar(self):
        return self._rows[0][0] if self._rows else None


class _FakeCursor:
    def __init__(self, rows, description):
        self._rows, self.description = rows, description
        self.cancelled = False
        self.closed = False
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)

    def fetchall(self):
        return list(self._rows)

    def cancel(self):
        self.cancelled = True

    def close(self):
        self.closed = True


class _FakeRaw:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class _FakeSAConn:
    """Just enough of a SQLAlchemy Connection: execute(text, params) routed
    to a responder keyed on a substring of the SQL, plus `info`, and the
    `.connection.dbapi_connection` path to the raw driver."""

    def __init__(self, responder, cursor=None):
        self._responder = responder
        self.info = {}
        self.executed = []
        self.closed = False
        self.connection = type("Fairy", (), {"dbapi_connection": _FakeRaw(cursor)})()

    def execute(self, stmt, params=None):
        sql = str(getattr(stmt, "text", stmt))
        self.executed.append((" ".join(sql.split()), dict(params or {})))
        return _Result(self._responder(" ".join(sql.split()), dict(params or {})))

    def close(self):
        self.closed = True


class _FakeEngine:
    def __init__(self, conn):
        self._conn = conn

    def connect(self):
        return self._conn


def _install_engine(monkeypatch, conn, capture):
    def fake_get_engine(uri, connect_args=None, **kw):
        capture.append((uri, dict(connect_args or {})))
        return _FakeEngine(conn)

    monkeypatch.setattr(mod, "get_engine", fake_get_engine)


def _client(**overrides) -> TrinoClient:
    base = dict(host="coord.example.com", port=443, catalog="tpch", schema="sf1",
                user="svc@acct.galaxy.starburst.io", password="pw", protocol="https")
    base.update(overrides)
    return TrinoClient(**base)


# ---------- URI + connect args ---------- #


class TestConnectionShape:
    def test_https_scheme_is_passed_to_the_driver_regardless_of_port(self, monkeypatch):
        """The driver only infers TLS from port 443; an Enterprise coordinator
        on 8443 must still get https explicitly."""
        c = _client(port=8443)
        assert c._connect_args()["http_scheme"] == "https"
        assert c.trino_uri.startswith("trino://")
        assert ":8443/" in c.trino_uri

    def test_password_never_travels_over_plain_http(self):
        c = _client(protocol="http", port=8080)
        assert "pw" not in c.trino_uri
        assert c.trino_uri.startswith("trino://svc%40acct.galaxy.starburst.io@")
        assert c._connect_args()["http_scheme"] == "http"

    def test_password_is_in_the_identity_over_https(self):
        c = _client()
        assert "svc%40acct.galaxy.starburst.io:pw@" in c.trino_uri

    def test_token_auth_rides_as_access_token(self):
        c = _client(user="", password=None, access_token="eyJ.abc.def")
        assert "access_token=eyJ.abc.def" in c.trino_uri
        assert c.trino_uri.startswith("trino://coord.example.com:443/")

    def test_token_auth_requires_https(self):
        with pytest.raises(ValueError):
            _client(protocol="http", access_token="tok")

    def test_catalog_is_required(self):
        with pytest.raises(ValueError):
            _client(catalog="")

    def test_verify_off_and_ca_bundle(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod.tempfile, "gettempdir", lambda: str(tmp_path))
        assert "verify" not in _client()._connect_args()
        assert _client(verify_ssl=False)._connect_args()["verify"] is False
        pem = "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----"
        a = _client(ca_certificate=pem)._connect_args()["verify"]
        b = _client(ca_certificate=pem)._connect_args()["verify"]
        assert a == b and a.startswith(str(tmp_path))
        assert open(a).read().strip() == pem

    def test_role_timezone_and_source(self):
        args = _client(role="analyst", timezone="Europe/Berlin")._connect_args()
        assert args["roles"] == {"system": "analyst"}
        assert args["timezone"] == "Europe/Berlin"
        assert args["source"] == mod.QUERY_SOURCE
        assert "roles" not in _client()._connect_args()

    def test_identity_and_role_both_separate_pooled_engines(self, monkeypatch):
        """Pool keys are (uri, connect_args): two users, or one user under two
        roles, must never share an engine."""
        from app.data_sources.engine_pool import _key
        a, b = _client(user="alice"), _client(user="bob")
        assert _key(a.trino_uri, a._connect_args(), None) != _key(b.trino_uri, b._connect_args(), None)
        r1, r2 = _client(role="r1"), _client(role="r2")
        assert r1.trino_uri == r2.trino_uri
        assert _key(r1.trino_uri, r1._connect_args(), None) != _key(r2.trino_uri, r2._connect_args(), None)

    def test_connect_passes_uri_and_args_to_the_pool(self, monkeypatch):
        capture = []
        _install_engine(monkeypatch, _FakeSAConn(lambda sql, p: [[1]]), capture)
        c = _client(role="analyst")
        assert c.test_connection()["success"] is True
        (uri, args), = capture
        assert uri == c.trino_uri
        assert args == c._connect_args()

    def test_schema_list_parsing(self):
        c = _client(schema=" sf1, sf10 ,sf1 ")
        assert c._schemas == ["sf1", "sf10"]
        assert c.trino_uri.endswith("/tpch/sf1")
        assert _client(schema=None).trino_uri.endswith("/tpch")

    def test_additional_catalogs_exclude_the_primary(self):
        c = _client(additional_catalogs="tpcds, tpch, hive")
        assert c.catalogs == ["tpch", "tpcds", "hive"]


# ---------- execute_query + cancellation ---------- #


class TestExecuteQuery:
    def test_rows_become_a_dataframe_and_the_cursor_is_parked_for_cancel(self, monkeypatch):
        cur = _FakeCursor([[1, "a"], [2, "b"]], [("n", None), ("s", None)])
        parked = []
        conn = _FakeSAConn(lambda sql, p: [], cursor=cur)
        orig_execute = cur.execute

        def execute(sql, params=None):
            parked.append(conn.info.get("bow_trino_cursor"))
            orig_execute(sql, params)

        cur.execute = execute
        _install_engine(monkeypatch, conn, [])
        df = _client().execute_query("SELECT 1 AS n, 'a' AS s")
        assert isinstance(df, pd.DataFrame)
        assert list(df.columns) == ["n", "s"] and len(df) == 2
        assert parked == [cur], "cursor must be reachable from the connection while the query runs"
        assert "bow_trino_cursor" not in conn.info, "and released afterwards"
        assert cur.closed

    def test_sql_is_sent_verbatim_without_bind_parsing(self, monkeypatch):
        cur = _FakeCursor([], [])
        conn = _FakeSAConn(lambda sql, p: [], cursor=cur)
        _install_engine(monkeypatch, conn, [])
        sql = "SELECT * FROM t WHERE ts > timestamp '2024-01-01 12:30:00' AND x::varchar = 'a:b'"
        _client().execute_query(sql)
        assert cur.executed == [sql]

    def test_driver_errors_surface_as_runtime_error(self, monkeypatch):
        cur = _FakeCursor([], [])
        cur.execute = lambda *a, **k: (_ for _ in ()).throw(Exception("TABLE_NOT_FOUND"))
        _install_engine(monkeypatch, _FakeSAConn(lambda s, p: [], cursor=cur), [])
        with pytest.raises(RuntimeError, match="TABLE_NOT_FOUND"):
            _client().execute_query("SELECT 1")

    def test_cancellation_uses_the_parked_cursor(self):
        from app.data_sources.query_cancellation import _cancel

        cur = _FakeCursor([], [])
        conn = _FakeSAConn(lambda s, p: [], cursor=cur)
        conn.engine = type("E", (), {"dialect": type("D", (), {"name": "trino"})()})()
        conn.info["bow_trino_cursor"] = cur
        assert "cancelled" in _cancel(conn)
        assert cur.cancelled
        conn.info.clear()
        assert _cancel(conn).startswith("unsupported")


# ---------- discovery ---------- #


def _responder_for(catalog_tables):
    """catalog -> {(schema, table): {"type":..., "comment":..., "columns":[(name,type,comment)]}}"""

    def respond(sql, params):
        cat = None
        for c in catalog_tables:
            if f'"{c}".information_schema' in sql:
                cat = c
        schemas = [v for k, v in params.items() if k.startswith("s")]

        def wanted(schema):
            return not schemas or schema in schemas

        if cat and "information_schema.tables" in sql and "count(*)" in sql:
            return [[sum(1 for (s, _t) in catalog_tables[cat] if wanted(s))]]
        if cat and "information_schema.tables" in sql:
            return [[s, t, v["type"]] for (s, t), v in catalog_tables[cat].items() if wanted(s)]
        if "system.metadata.table_comments" in sql:
            cat = params["catalog"]
            return [[s, t, v["comment"]] for (s, t), v in catalog_tables[cat].items()
                    if v.get("comment") and wanted(s)]
        if cat and "information_schema.columns" in sql:
            rows = []
            for (s, t), v in catalog_tables[cat].items():
                if not wanted(s):
                    continue
                for name, dtype, comment in v["columns"]:
                    rows.append([s, t, name, dtype, comment] if "comment" in sql else [s, t, name, dtype])
            return rows
        if "SELECT 1" in sql:
            return [[1]]
        raise AssertionError(f"unexpected SQL: {sql}")

    return respond


CATALOGS = {
    "tpch": {
        ("sf1", "orders"): {"type": "BASE TABLE", "comment": "All orders",
                            "columns": [("orderkey", "bigint", None), ("status", "varchar(1)", "O/F/P")]},
        ("sf1", "v_open"): {"type": "VIEW", "comment": None, "columns": [("orderkey", "bigint", None)]},
        ("sf10", "orders"): {"type": "BASE TABLE", "comment": None, "columns": [("orderkey", "bigint", None)]},
    },
    "hive": {
        ("sales", "fact"): {"type": "BASE TABLE", "comment": None, "columns": [("amount", "decimal(10,2)", None)]},
    },
}


class TestDiscovery:
    def test_tables_are_named_for_the_agent_and_carry_comments_and_view_flags(self, monkeypatch):
        conn = _FakeSAConn(_responder_for(CATALOGS))
        _install_engine(monkeypatch, conn, [])
        tables = {t.name: t for t in _client(schema=None, additional_catalogs="hive").get_tables()}
        # primary catalog -> schema.table; other catalogs -> catalog.schema.table
        assert set(tables) == {"sf1.orders", "sf1.v_open", "sf10.orders", "hive.sales.fact"}
        assert tables["sf1.orders"].description == "All orders"
        assert [c.description for c in tables["sf1.orders"].columns] == [None, "O/F/P"]
        assert tables["sf1.v_open"].metadata_json["type"] == "VIEW"
        assert tables["hive.sales.fact"].metadata_json == {"catalog": "hive", "schema": "sales", "type": "BASE TABLE"}
        assert [c.dtype for c in tables["hive.sales.fact"].columns] == ["decimal(10,2)"]

    def test_configured_schemas_filter_every_catalog_read(self, monkeypatch):
        conn = _FakeSAConn(_responder_for(CATALOGS))
        _install_engine(monkeypatch, conn, [])
        names = {t.name for t in _client(schema="sf1").get_tables()}
        assert names == {"sf1.orders", "sf1.v_open"}
        # every statement that touches a schema column was bound to the list
        for sql, params in conn.executed:
            if "information_schema" in sql or "table_comments" in sql:
                assert params.get("s0") == "sf1", sql

    def test_system_schema_is_never_indexed(self, monkeypatch):
        conn = _FakeSAConn(_responder_for(CATALOGS))
        _install_engine(monkeypatch, conn, [])
        _client(schema=None).get_tables()
        assert all("information_schema'" in sql or "NOT IN" in sql for sql, _ in conn.executed
                   if "information_schema.columns" in sql)

    def test_catalog_names_are_quoted_identifiers(self, monkeypatch):
        cats = {"my-lake": {("s", "t"): {"type": "BASE TABLE", "comment": None, "columns": [("a", "int", None)]}}}
        conn = _FakeSAConn(_responder_for(cats))
        _install_engine(monkeypatch, conn, [])
        tables = _client(catalog="my-lake", schema=None).get_tables()
        assert [t.name for t in tables] == ["s.t"]
        assert any('"my-lake".information_schema.columns' in sql for sql, _ in conn.executed)

    def test_enriched_failure_falls_back_to_the_basic_listing(self, monkeypatch):
        base = _responder_for(CATALOGS)

        def flaky(sql, params):
            if "comment" in sql and "information_schema.columns" in sql:
                raise RuntimeError("Column 'comment' cannot be resolved")  # old Presto
            return base(sql, params)

        conn = _FakeSAConn(flaky)
        _install_engine(monkeypatch, conn, [])
        tables = {t.name: t for t in _client(schema=None).get_tables()}
        assert set(tables) == {"sf1.orders", "sf1.v_open", "sf10.orders"}
        assert tables["sf1.orders"].description is None

    def test_count_tables_matches_discovery_and_reads_no_columns(self, monkeypatch):
        conn = _FakeSAConn(_responder_for(CATALOGS))
        _install_engine(monkeypatch, conn, [])
        c = _client(schema=None, additional_catalogs="hive")
        assert c.count_tables() == len(c.get_tables()) == 4
        count_stmts = [sql for sql, _ in conn.executed if "count(*)" in sql]
        assert count_stmts and all("information_schema.columns" not in sql for sql in count_stmts)
        assert _client(schema="sf1").count_tables() == 2

    def test_description_tells_the_agent_the_naming_convention(self):
        d = _client(additional_catalogs="hive").description
        assert "schema.table" in d and "catalog.schema.table" in d
        assert "tpch" in d and "hive" in d
        assert "double quotes" in d


# ---------- registry ---------- #


class TestRegistry:
    def test_trino_and_starburst_share_the_client_with_different_defaults(self):
        from app.schemas.data_source_registry import get_entry, resolve_client_class

        assert resolve_client_class("trino") is TrinoClient
        assert resolve_client_class("starburst") is TrinoClient
        trino = get_entry("trino").config_schema.model_json_schema()["properties"]
        sb = get_entry("starburst").config_schema.model_json_schema()["properties"]
        assert (trino["port"]["default"], trino["protocol"]["default"]) == (8080, "http")
        assert (sb["port"]["default"], sb["protocol"]["default"]) == (443, "https")
        for props in (trino, sb):
            assert {"catalog", "schema", "additional_catalogs", "role", "verify_ssl", "ca_certificate", "timezone"} <= set(props)
        for t in ("trino", "starburst"):
            assert set(get_entry(t).credentials_auth.by_auth) == {"userpass", "token"}
        assert get_entry("starburst").setup_guide

    def test_presto_entry_is_the_same_client_family(self):
        from app.data_sources.clients.presto_client import PrestoClient

        p = PrestoClient(host="h", port=8080, catalog="c", schema="s", user="u", password="pw")
        assert isinstance(p, TrinoClient)
        assert p.presto_uri == p.trino_uri
        assert "pw" not in p.presto_uri  # plain HTTP default: no credentials on the wire
