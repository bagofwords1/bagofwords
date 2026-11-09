"""Snowflake queries BOW sends outside a chat are tagged too (BOW-115).

Connection checks and catalog indexing run as the BOW service user just like
agent queries do; a Snowflake admin must be able to tell them apart and tie
them to the BOW connection and organization. These go through the real
connection routes; only the Snowflake driver is replaced by the fake server
(tests/fixtures/fake_snowflake.py). Catalog discovery reads the fake's tables
through the client's own connection, because Snowflake's INFORMATION_SCHEMA
does not exist on the fake.
"""
import json
import sqlite3

import pytest

from app.ai.prompt_formatters import Table, TableColumn
from app.data_sources.clients.snowflake_client import SnowflakeClient
from tests.fixtures.fake_snowflake import FakeSnowflakeServer


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    server = FakeSnowflakeServer(tmp_path / "wh.db", pool_size=2)
    server.install(monkeypatch)
    with sqlite3.connect(server.db_path) as c:
        c.execute("CREATE TABLE orders (id INTEGER, amount REAL)")

    def get_tables(self):
        with self.connect() as conn:
            names = [r[0] for r in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return [
                Table(name=n, pks=[], fks=[], columns=[
                    TableColumn(name=r[1], dtype=r[2] or "TEXT")
                    for r in conn.exec_driver_sql(f"PRAGMA table_info('{n}')")
                ])
                for n in names
            ]

    monkeypatch.setattr(SnowflakeClient, "get_tables", get_tables)
    return server


def _snowflake(create_connection, token, org_id, **config):
    return create_connection(
        name="Warehouse",
        type="snowflake",
        config={"account": "acct", "warehouse": "WH", "database": "DB", **config},
        credentials={"user": "svc", "password": "pw"},
        user_token=token,
        org_id=org_id,
    )


def _tags(server, since=0):
    return [json.loads(r["query_tag"]) for r in server.rows()[since:] if r["query_tag"]]


@pytest.mark.e2e
def test_connection_checks_and_indexing_are_tagged_with_org_and_connection(
    warehouse, test_client, create_connection, create_user, login_user, whoami, wait_for_connection_indexing,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    conn = _snowflake(create_connection, token, org_id, query_tag_label="finance")
    run = wait_for_connection_indexing(connection_id=conn["id"], user_token=token, org_id=org_id)
    assert run and run["status"] == "completed", run
    statements = warehouse.rows()
    assert statements, "creating the connection should have reached the warehouse"
    assert all(r["query_tag"] for r in statements), [r["query_text"][:80] for r in statements if not r["query_tag"]]
    assert all(t["org_id"] == org_id for t in _tags(warehouse)), _tags(warehouse)
    indexing = [t for t in _tags(warehouse) if t["source"] == "indexing"]
    assert indexing, _tags(warehouse)
    assert all(t["app"] == "bow" and t["org_id"] == org_id for t in indexing)
    assert any(t.get("connection_id") == conn["id"] for t in indexing)
    assert all(t.get("label") == "finance" for t in _tags(warehouse))

    before = len(warehouse.rows())
    resp = test_client.post(
        f"/api/connections/{conn['id']}/test",
        headers={"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json().get("success") is True, resp.json()
    probes = _tags(warehouse, since=before)
    assert probes and all(t["source"] == "connection_test" for t in probes), probes


@pytest.mark.e2e
def test_tagging_can_be_turned_off_per_connection(
    warehouse, create_connection, create_user, login_user, whoami, wait_for_connection_indexing,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    conn = _snowflake(create_connection, token, org_id, query_tag_enabled=False)
    wait_for_connection_indexing(connection_id=conn["id"], user_token=token, org_id=org_id)
    assert warehouse.rows(), "creating the connection should have reached the warehouse"
    assert not [r for r in warehouse.rows(include_session_statements=True) if "QUERY_TAG" in r["query_text"]]


@pytest.mark.e2e
@pytest.mark.parametrize("label", ["x'); DROP TABLE t; --", "a" * 101, "line\nbreak"])
def test_query_tag_label_rejects_values_that_cannot_go_in_a_tag(
    warehouse, test_client, create_user, login_user, whoami, label,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    resp = test_client.post(
        "/api/connections",
        json={
            "name": "Warehouse", "type": "snowflake",
            "config": {"account": "acct", "warehouse": "WH", "database": "DB", "query_tag_label": label},
            "credentials": {"user": "svc", "password": "pw"},
            "auth_policy": "system_only",
        },
        headers={"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)},
    )
    assert 400 <= resp.status_code < 500, resp.text
    assert not warehouse.rows(), "an invalid label must be rejected before connecting"


@pytest.mark.e2e
def test_editing_a_connection_rejects_an_unfit_label_and_keeps_a_fit_one(
    warehouse, test_client, create_connection, create_user, login_user, whoami, wait_for_connection_indexing,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
    conn = _snowflake(create_connection, token, org_id)
    wait_for_connection_indexing(connection_id=conn["id"], user_token=token, org_id=org_id)
    base = {"account": "acct", "warehouse": "WH", "database": "DB"}

    bad = test_client.put(f"/api/connections/{conn['id']}", headers=headers,
                          json={"config": {**base, "query_tag_label": "it's; bad"}})
    assert 400 <= bad.status_code < 500, bad.text

    good = test_client.put(f"/api/connections/{conn['id']}", headers=headers,
                           json={"config": {**base, "query_tag_label": "ops/eu-west"}})
    assert good.status_code == 200, good.text
    before = len(warehouse.rows())
    resp = test_client.post(f"/api/connections/{conn['id']}/test", headers=headers)
    assert resp.status_code == 200, resp.text
    assert {t.get("label") for t in _tags(warehouse, since=before)} == {"ops/eu-west"}
