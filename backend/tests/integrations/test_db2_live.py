"""Live Db2 tests: Db2 LUW for real, z/OS and IBM i simulated on the same server.

Needs a running Db2 LUW server (the free Community image is enough):

    docker run -d --name db2 --privileged -p 50000:50000 -e LICENSE=accept \\
        -e DB2INST1_PASSWORD=<pw> -e DBNAME=TESTDB icr.io/db2_community/db2:latest
    DB2_TEST_DSN="DATABASE=TESTDB;HOSTNAME=127.0.0.1;PORT=50000;PROTOCOL=TCPIP;UID=db2inst1;PWD=<pw>;" \\
        uv run pytest tests/integrations/test_db2_live.py -v

Skipped when DB2_TEST_DSN is unset. `db2_sim.seed` (idempotent) creates the
SALES schema and the z/OS / IBM i catalog stand-ins; see its docstring for
what the simulation does and does not cover.
"""

from __future__ import annotations

import os

import pytest

DSN = os.environ.get("DB2_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="DB2_TEST_DSN not set")


def _dsn_params() -> dict:
    return {k.upper(): v for k, v in (p.split("=", 1) for p in DSN.strip(";").split(";"))}


@pytest.fixture(scope="module")
def seeded():
    from tests.integrations.db2_sim import ZOS_SIM_SCHEMA, seed

    seed(DSN)
    return ZOS_SIM_SCHEMA


def _db2_client(platform="auto", **kw):
    from app.data_sources.clients.db2_client import Db2Client

    p = _dsn_params()
    return Db2Client(host=p["HOSTNAME"], port=int(p["PORT"]), database=p["DATABASE"],
                     user=p["UID"], password=p["PWD"], platform=platform, **kw)


@pytest.fixture(scope="module")
def mapepire(seeded):
    import ibm_db_dbi

    from tests.mocks.fake_mapepire import FakeMapepireServer

    srv = FakeMapepireServer(lambda: ibm_db_dbi.connect(DSN, "", ""), first_batch=50).start()
    yield srv
    srv.stop()


def _assert_sales_model(tables):
    by = {t.name: t for t in tables}
    assert {"SALES.CUSTOMERS", "SALES.ORDERS", "SALES.ORDER_LINES", "SALES.PRODUCTS"} <= set(by)
    lines = by["SALES.ORDER_LINES"]
    assert [p.name for p in lines.pks] == ["ORDER_ID", "LINE_NO"]
    assert {(f.column.name, f.references_name) for f in lines.fks} == {
        ("ORDER_ID", "SALES.ORDERS"), ("PRODUCT_ID", "SALES.PRODUCTS")}
    assert "revenue" in (lines.description or "").lower()
    assert dict((c.name, c.dtype) for c in lines.columns)["UNIT_PRICE"] == "DECIMAL(9,2)"
    status = next(c for c in by["SALES.ORDERS"].columns if c.name == "STATUS")
    assert status.description and "shipped" in status.description


def test_luw_detected_and_discovered(seeded):
    c = _db2_client(schema="SALES")
    result = c.test_connection()
    assert result["success"], result
    assert c.effective_platform == "luw"
    _assert_sales_model(c.get_schemas())


def test_zos_catalog(seeded, monkeypatch):
    from app.data_sources.clients.db2_client import Db2Client

    monkeypatch.setattr(Db2Client, "catalog_schemas", {"zos": seeded})
    c = _db2_client(platform="zos", schema="SALES")
    assert c.test_connection()["success"]
    _assert_sales_model(c.get_schemas())


def test_ibmi_catalog_over_ibm_db(seeded):
    c = _db2_client(platform="ibmi", schema="SALES")
    _assert_sales_model(c.get_schemas())


def test_ibmi_over_mapepire(mapepire):
    from app.data_sources.clients.ibmi_client import IbmiClient

    c = IbmiClient(host="127.0.0.1", port=mapepire.port, user=mapepire.user,
                   password=mapepire.password, ssl_ca=mapepire.cert_pem, schema="SALES")
    assert c.test_connection()["success"]
    _assert_sales_model(c.get_schemas())
    df = c.execute_query("SELECT * FROM SALES.ORDER_LINES")
    assert len(df) > 50  # more than one Mapepire batch


def test_agent_style_queries(seeded):
    c = _db2_client()
    df = c.execute_query(
        "SELECT c.REGION, SUM(l.QUANTITY * l.UNIT_PRICE) AS REVENUE "
        "FROM SALES.ORDER_LINES l JOIN SALES.ORDERS o ON o.ORDER_ID = l.ORDER_ID "
        "JOIN SALES.CUSTOMERS c ON c.CUSTOMER_ID = o.CUSTOMER_ID "
        "WHERE o.STATUS <> 'C' AND o.ORDER_DATE >= DATE('2026-01-01') - 1 MONTH "
        "GROUP BY c.REGION ORDER BY REVENUE DESC FETCH FIRST 5 ROWS ONLY"
    )
    assert list(df.columns) == ["REGION", "REVENUE"]
    assert set(df["REGION"].str.strip()) == {"EMEA", "AMER", "APAC"}


def test_preview_sql_runs(seeded):
    from app.models.connection_table import ConnectionTable
    from app.services.table_preview_service import preview_sql

    table = ConnectionTable(name="SALES.ORDERS", metadata_json={"schema": "SALES"},
                            columns=[{"name": "ORDER_ID"}, {"name": "STATUS"}])
    df = _db2_client().execute_query(preview_sql("db2", table, 7))
    assert len(df) == 7
