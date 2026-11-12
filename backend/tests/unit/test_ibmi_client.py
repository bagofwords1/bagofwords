"""Unit tests for the IBM i connector (Db2 for IBM i over Mapepire).

The boundary is the network: IBM's real `mapepire-python` client talks TLS
WebSocket to `tests/mocks/fake_mapepire.py`, which answers with an sqlite
database laid out like the IBM i catalog (`QSYS2.*`). So these cover the
client's real request flow — TLS verification, Basic auth, job properties,
result paging and terse rows — plus the IBM i catalog SQL.
"""

from __future__ import annotations

import pytest

from tests.mocks import db2_catalog_sqlite as fakecat
from tests.mocks.fake_mapepire import FakeMapepireServer

from app.data_sources.clients.ibmi_client import IbmiClient


@pytest.fixture
def server(tmp_path):
    fakecat.build(str(tmp_path), "ibmi")
    # A small first batch so every multi-row result needs follow-up fetches.
    srv = FakeMapepireServer(lambda: fakecat.connect(str(tmp_path), "ibmi"),
                             user="ANALYST", password="s3cret", first_batch=2).start()
    yield srv
    srv.stop()


def _client(srv, **kw) -> IbmiClient:
    args = dict(host="127.0.0.1", port=srv.port, user="ANALYST", password="s3cret", ssl_ca=srv.cert_pem)
    args.update(kw)
    return IbmiClient(**args)


def test_connects_with_the_pasted_server_certificate(server):
    assert _client(server).test_connection()["success"] is True


def test_unknown_self_signed_certificate_is_rejected_when_verifying(server):
    # Mapepire's client would download and trust the server's own certificate
    # if handed no CA; verification must not degrade to that.
    result = _client(server, ssl_ca=None).test_connection()
    assert result["success"] is False
    assert "verify failed" in result["message"].lower()


def test_verification_can_be_turned_off(server):
    assert _client(server, ssl_ca=None, verify_ssl=False).test_connection()["success"] is True


def test_wrong_password_fails(server):
    result = _client(server, password="wrong").test_connection()
    assert result["success"] is False


def test_unreachable_server_mentions_mapepire():
    result = IbmiClient(host="127.0.0.1", port=1, user="u", password="p", verify_ssl=False).test_connection()
    assert result["success"] is False
    assert "Mapepire" in result["message"]


def test_job_uses_sql_naming_and_read_only_access(server):
    _client(server, schema="sales").test_connection()
    props = dict(p.split("=", 1) for p in server.connect_props[-1].split(";"))
    assert props["naming"] == "sql"
    assert props["access"] == "read only"
    assert props["libraries"] == "SALES"


def test_discovers_ibmi_catalog(server):
    tables = {t.name: t for t in _client(server).get_schemas()}
    assert {"SALES.CUSTOMERS", "SALES.ORDERS", "SALES.V_BIG_ORDERS", "OPS.JOBS"} <= set(tables)
    assert not any(name.startswith("Q") for name in tables)  # IBM-supplied libraries hidden
    assert tables["OPS.JOBS"].metadata_json["object_type"] == "physical_file"
    orders = tables["SALES.ORDERS"]
    assert [p.name for p in orders.pks] == ["ORDER_ID"]
    assert [(f.column.name, f.references_name) for f in orders.fks] == [("CUSTOMER_ID", "SALES.CUSTOMERS")]
    assert tables["SALES.CUSTOMERS"].description == "Customer master"


def test_query_fetches_every_row_across_batches(server):
    df = _client(server).execute_query("SELECT ORDER_ID, AMOUNT FROM SALES.ORDERS ORDER BY ORDER_ID")
    assert list(df.columns) == ["ORDER_ID", "AMOUNT"]
    assert df["ORDER_ID"].tolist() == [r[0] for r in fakecat.DATA[("SALES", "ORDERS")]]


def test_same_named_columns_are_both_kept(server):
    df = _client(server).execute_query(
        "SELECT o.CUSTOMER_ID, c.CUSTOMER_ID, c.NAME FROM SALES.ORDERS o "
        "JOIN SALES.CUSTOMERS c ON c.CUSTOMER_ID = o.CUSTOMER_ID ORDER BY o.ORDER_ID"
    )
    assert df.shape == (len(fakecat.DATA[("SALES", "ORDERS")]), 3)
    assert df.iloc[0].tolist() == [1, 1, "Acme"]


def test_query_error_is_readable(server):
    with pytest.raises(RuntimeError) as exc:
        _client(server).execute_query("SELECT * FROM SALES.NOPE")
    assert "NOPE" in str(exc.value)
    assert "{'error'" not in str(exc.value)


def test_description_uses_sql_naming():
    d = IbmiClient(host="as400.example.com", user="u", password="p", schema="SALES").description
    assert "LIBRARY.TABLE" in d and "FETCH FIRST" in d
