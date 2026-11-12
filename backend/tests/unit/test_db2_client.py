"""Unit tests for the IBM Db2 connector (Db2 LUW, Db2 for z/OS, Db2 for IBM i).

The driver boundary (`ibm_db_dbi`) is replaced by a fake whose connections are
sqlite databases laid out like each product's system catalog
(`tests/mocks/db2_catalog_sqlite.py`). Everything above the driver runs for
real: the pooled SQLAlchemy dialect, product detection from the DBMS name,
the catalog SQL, row mapping, the read-only guard and the registry wiring.
Live Db2 coverage is in `tests/integrations/test_db2_live.py`.
"""

from __future__ import annotations

import inspect
import sqlite3
import sys
import types
import uuid

import pandas as pd
import pytest

from tests.mocks import db2_catalog_sqlite as fakecat

from app.data_sources.clients.db2_client import Db2Client

DBMS_NAMES = {"luw": "DB2/LINUXX8664", "zos": "DB2", "ibmi": "AS"}


class _Conn:
    """sqlite connection posing as an ibm_db_dbi.Connection."""

    def __init__(self, raw, dbms_name):
        self._raw = raw
        self.dbms_name = dbms_name
        self.dbms_ver = "12.01.0500"

    def cursor(self):
        return self._raw.cursor()

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()


@pytest.fixture
def fake_driver(monkeypatch, tmp_path):
    """Install a fake `ibm_db_dbi`; returns a function (platform) -> state dict."""
    state: dict = {"dsns": []}

    def use(platform: str, connect_error: Exception | None = None):
        fakecat.build(str(tmp_path), platform)
        state.update(platform=platform, error=connect_error)
        return state

    def _connect(dsn, user="", password=""):
        state["dsns"].append(dsn)
        if state.get("error"):
            raise state["error"]
        return _Conn(fakecat.connect(str(tmp_path), state["platform"]), DBMS_NAMES[state["platform"]])

    mod = types.ModuleType("ibm_db_dbi")
    mod.connect = _connect
    mod.paramstyle = "qmark"
    for name in ("Error", "Warning", "InterfaceError", "DatabaseError", "DataError",
                 "OperationalError", "IntegrityError", "InternalError",
                 "ProgrammingError", "NotSupportedError"):
        setattr(mod, name, getattr(sqlite3, name))
    monkeypatch.setitem(sys.modules, "ibm_db_dbi", mod)
    return use


def _client(**kw) -> Db2Client:
    # A unique host per test gives each test its own pooled engine.
    defaults = dict(host=f"db2-{uuid.uuid4().hex[:8]}.test", port=50000, database="BOWDB",
                    user="reader", password="pw")
    defaults.update(kw)
    return Db2Client(**defaults)


def _by_name(tables):
    return {t.name: t for t in tables}


# ---------------------------------------------------------------- discovery --

@pytest.mark.parametrize("platform", ["luw", "zos", "ibmi"])
class TestDiscoveryAcrossProducts:
    def test_detects_product_and_reports_it(self, fake_driver, platform):
        fake_driver(platform)
        c = _client()
        result = c.test_connection()
        assert result["success"] is True
        assert c.effective_platform == platform

    def test_lists_user_tables_and_views_but_not_system_schemas(self, fake_driver, platform):
        fake_driver(platform)
        tables = _by_name(_client().get_schemas())
        assert {"SALES.CUSTOMERS", "SALES.ORDERS", "SALES.V_BIG_ORDERS", "OPS.JOBS"} <= set(tables)
        sys_schema, sys_table = fakecat.SYSTEM_TABLE[platform]
        assert f"{sys_schema}.{sys_table}" not in tables
        assert all(t.metadata_json["db2_platform"] == platform for t in tables.values())
        assert tables["SALES.V_BIG_ORDERS"].metadata_json["object_type"] == "view"

    def test_columns_keep_catalog_order_with_readable_types_and_comments(self, fake_driver, platform):
        fake_driver(platform)
        orders = _by_name(_client().get_schemas())["SALES.ORDERS"]
        assert [c.name for c in orders.columns] == ["ORDER_ID", "CUSTOMER_ID", "AMOUNT"]
        amount = orders.columns[2]
        assert amount.dtype == "DECIMAL(9,2)"
        assert amount.description == "Order total"
        assert orders.description is None  # blank remarks are not a description

    def test_primary_and_foreign_keys(self, fake_driver, platform):
        fake_driver(platform)
        tables = _by_name(_client().get_schemas())
        assert [p.name for p in tables["SALES.CUSTOMERS"].pks] == ["CUSTOMER_ID"]
        fks = tables["SALES.ORDERS"].fks
        assert [(f.column.name, f.references_name, f.references_column.name) for f in fks] == [
            ("CUSTOMER_ID", "SALES.CUSTOMERS", "CUSTOMER_ID")
        ]
        # FK targets use the same naming as Table.name, so the schema context can resolve them.
        assert fks[0].references_name in tables

    def test_schema_list_limits_discovery(self, fake_driver, platform):
        fake_driver(platform)
        tables = _client(schema="ops").get_schemas()  # unquoted -> uppercased like Db2
        assert {t.name for t in tables} == {"OPS.JOBS"}

    def test_listing_a_system_schema_explicitly_includes_it(self, fake_driver, platform):
        fake_driver(platform)
        sys_schema, sys_table = fakecat.SYSTEM_TABLE[platform]
        names = {t.name for t in _client(schema=sys_schema).get_schemas()}
        assert names == {f"{sys_schema}.{sys_table}"}


def test_forced_platform_overrides_detection(fake_driver):
    # The server says LUW, but the admin forces IBM i: the IBM i catalog is
    # read, which this fake LUW server does not have, so discovery fails
    # instead of silently reading the wrong catalog.
    fake_driver("luw")
    c = _client(platform="ibmi")
    assert c.test_connection()["success"] is True
    assert c.effective_platform == "ibmi"
    with pytest.raises(Exception):
        c.get_schemas()


def test_keys_are_best_effort(fake_driver, tmp_path):
    fake_driver("luw")
    conn = fakecat.connect(str(tmp_path), "luw")
    conn.execute("DROP TABLE SYSCAT.KEYCOLUSE")  # e.g. no SELECT grant on the key views
    conn.commit()
    conn.close()
    tables = _by_name(_client().get_schemas())
    assert "SALES.ORDERS" in tables and tables["SALES.ORDERS"].columns
    assert tables["SALES.ORDERS"].pks == [] and tables["SALES.ORDERS"].fks == []


# ------------------------------------------------------------------ queries --

def test_execute_query_returns_dataframe(fake_driver):
    fake_driver("luw")
    df = _client().execute_query("SELECT CUSTOMER_ID, SUM(AMOUNT) AS TOTAL FROM SALES.ORDERS GROUP BY CUSTOMER_ID")
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == ["CUSTOMER_ID", "TOTAL"]
    assert len(df) == 3


def test_query_error_is_the_driver_message_not_sqlalchemy_noise(fake_driver):
    fake_driver("luw")
    with pytest.raises(RuntimeError) as exc:
        _client().execute_query("SELECT * FROM SALES.NOPE")
    assert "NOPE" in str(exc.value)
    assert "[SQL:" not in str(exc.value) and "sqlalche.me" not in str(exc.value)


def test_missing_db2_connect_license_is_explained(fake_driver):
    fake_driver("zos", connect_error=sqlite3.OperationalError(
        "[IBM][CLI Driver] SQL1598N  An attempt to connect to the database server failed "
        "because of a licensing problem.  SQLSTATE=42968"))
    result = _client(port=446).test_connection()
    assert result["success"] is False
    assert "SQL1598N" in result["message"] and "Db2 Connect" in result["message"]


def test_missing_driver_is_explained(monkeypatch):
    monkeypatch.setitem(sys.modules, "ibm_db_dbi", None)  # import fails, as on Linux ARM64
    result = _client().test_connection()
    assert result["success"] is False
    assert "ibm_db" in result["message"]


# -------------------------------------------------------------- connection --

def test_tls_and_schema_reach_the_driver(fake_driver):
    state = fake_driver("luw")
    pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----"
    _client(ssl=True, ssl_ca=pem, schema="SALES, OPS").test_connection()
    dsn = state["dsns"][-1]
    params = dict(p.split("=", 1) for p in dsn.strip(";").split(";"))
    assert params["SECURITY"] == "SSL"
    with open(params["SSLServerCertificate"]) as f:
        assert f.read().strip() == pem
    assert params["CURRENTSCHEMA"] == "SALES"
    assert params["DATABASE"] == "BOWDB" and params["PORT"] == "50000"


def test_plain_connection_has_no_tls(fake_driver):
    state = fake_driver("luw")
    _client().test_connection()
    assert "SECURITY=" not in state["dsns"][-1]


def test_password_with_semicolon_is_escaped(fake_driver):
    state = fake_driver("luw")
    _client(password="pa;ss").test_connection()
    assert "PWD={pa;ss};" in state["dsns"][-1]


# ---------------------------------------------------------------- read-only --

READS = [
    "SELECT * FROM SALES.ORDERS FETCH FIRST 10 ROWS ONLY",
    "WITH t AS (SELECT * FROM SALES.ORDERS) SELECT COUNT(*) FROM t",
    "SELECT * FROM SALES.ORDERS WHERE ORDER_DATE >= CURRENT DATE - 30 DAYS",
    "VALUES CURRENT DATE",
    "SELECT NOTE FROM T WHERE NOTE = 'delete; drop table x'",
]
WRITES = [
    "DELETE FROM SALES.ORDERS",
    "UPDATE SALES.ORDERS SET AMOUNT = 0",
    "SELECT * FROM FINAL TABLE (INSERT INTO SALES.ORDERS VALUES (1, 1, 1))",
    "SELECT 1 FROM SYSIBM.SYSDUMMY1; DROP TABLE SALES.ORDERS",
    "CALL SYSPROC.ADMIN_CMD('REORG TABLE SALES.ORDERS')",
    "SELECT QSYS2.QCMDEXC('DLTLIB SALES') FROM SYSIBM.SYSDUMMY1",
    "LOCK TABLE SALES.ORDERS IN EXCLUSIVE MODE",
    "SET CURRENT SCHEMA OPS",
    "MERGE INTO SALES.ORDERS o USING (VALUES 1) v(x) ON 1=0 WHEN NOT MATCHED THEN INSERT VALUES (1,1,1)",
]


@pytest.mark.parametrize("client_cls", ["db2", "ibmi"])
@pytest.mark.parametrize("sql", READS)
def test_read_only_guard_allows_reads(client_cls, sql):
    from app.ai.code_execution.sql_guard import ensure_read_only
    from app.data_sources.clients.ibmi_client import IbmiClient

    client = _client() if client_cls == "db2" else IbmiClient(host="h", user="u", password="p")
    ensure_read_only(client, sql)


@pytest.mark.parametrize("client_cls", ["db2", "ibmi"])
@pytest.mark.parametrize("sql", WRITES)
def test_read_only_guard_refuses_writes(client_cls, sql):
    from app.ai.code_execution.sql_guard import ReadOnlyViolation, ensure_read_only
    from app.data_sources.clients.ibmi_client import IbmiClient

    client = _client() if client_cls == "db2" else IbmiClient(host="h", user="u", password="p")
    with pytest.raises(ReadOnlyViolation):
        ensure_read_only(client, sql)


# ------------------------------------------------------------------ prompts --

@pytest.mark.parametrize("platform", ["luw", "zos", "ibmi"])
def test_description_teaches_db2_row_limits(platform):
    d = _client(platform=platform).description
    assert "FETCH FIRST" in d
    assert "SYSIBM.SYSDUMMY1" in d


def test_preview_uses_fetch_first():
    from app.models.connection_table import ConnectionTable
    from app.services.table_preview_service import preview_sql

    table = ConnectionTable(name="SALES.ORDERS", metadata_json={"schema": "SALES"}, columns=[])
    for conn_type in ("db2", "ibmi"):
        sql = preview_sql(conn_type, table, 25)
        assert sql.endswith("FETCH FIRST 25 ROWS ONLY")
        assert "LIMIT" not in sql and "TOP" not in sql


# ----------------------------------------------------------------- registry --

@pytest.mark.parametrize("type_,cls_path,config,creds", [
    ("db2", "app.data_sources.clients.db2_client.Db2Client", "Db2Config", "Db2Credentials"),
    ("ibmi", "app.data_sources.clients.ibmi_client.IbmiClient", "IbmiConfig", "IbmiCredentials"),
])
def test_registry_wiring(type_, cls_path, config, creds):
    import importlib

    from app.schemas import data_source_registry as reg
    from app.schemas.data_sources import configs

    entry = reg.get_entry(type_)
    assert entry.client_path == cls_path
    assert entry.config_schema is getattr(configs, config)
    assert reg.credentials_schema_for(type_, "userpass") is getattr(configs, creds)
    cls = reg.resolve_client_class(type_)
    module, name = cls_path.rsplit(".", 1)
    assert cls is getattr(importlib.import_module(module), name)
    # The service builds the client from config + credential fields by name.
    params = set(inspect.signature(cls.__init__).parameters) - {"self"}
    fields = set(getattr(configs, config).model_fields) | set(getattr(configs, creds).model_fields)
    assert fields <= params


@pytest.mark.parametrize("installed", [True, False])
def test_db2_listed_only_where_the_driver_exists(monkeypatch, installed):
    import importlib.util

    from app.schemas import data_source_registry

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a, **k: (object() if installed else None) if name == "ibm_db" else real(name, *a, **k))
    types_ = {e["type"] for e in data_source_registry.list_available_data_sources()}
    assert ("db2" in types_) is installed
    assert "ibmi" in types_  # pure-Python driver: available everywhere
    assert data_source_registry.resolve_client_class("db2") is Db2Client
