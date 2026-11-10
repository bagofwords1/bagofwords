"""Adding (and removing) databases on a Snowflake connection.

Contract, through the real connection and agent routes:
  * a one-database connection indexes tables as SCHEMA.TABLE;
  * listing a second database re-indexes every table as DATABASE.SCHEMA.TABLE,
    and a table that existed before keeps its identity: same row, still enabled
    for the agent, so selections and history survive;
  * removing the extra database brings the short names back, again in place;
  * the clients agents receive enforce the selection by full name: enabled
    tables of both databases can be joined, a same-named table in the other
    database does not pass for an enabled one.

Snowflake is faked at the driver boundary (tests/fixtures/fake_snowflake_catalog.py).
"""
import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.ai.code_execution.table_scope import TableScopeViolation, enforce_table_scope
from app.dependencies import async_session_maker
from app.models.data_source import DataSource
from app.services.data_source_service import DataSourceService
from tests.fixtures.fake_snowflake_catalog import FakeSnowflakeAccount

pytestmark = pytest.mark.e2e

ACCOUNT = {
    "DB_A": {"SALES": {"ORDERS": ["ID", "CUSTOMER_ID"], "SECRETS": ["ID"]}},
    "DB_B": {"CRM": {"CUSTOMERS": ["ID", "NAME"]}, "SALES": {"ORDERS": ["ID"]}},
}


@pytest.fixture
def account(monkeypatch):
    acct = FakeSnowflakeAccount(ACCOUNT)
    acct.install(monkeypatch)
    return acct


@pytest.fixture
def admin(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    return token, whoami(token)["organizations"][0]["id"]


def _tables_by_name(get_connection_tables, conn_id, token, org_id):
    return {t["name"]: t for t in get_connection_tables(connection_id=conn_id, user_token=token, org_id=org_id)}


def _agent_tables(get_data_source, ds_id, token, org_id, test_client):
    resp = test_client.get(
        f"/api/data_sources/{ds_id}/full_schema",
        params={"page_size": 100},
        headers={"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    rows = body.get("tables") or body.get("items") or []
    return {t["name"]: t for t in rows}


def _set_databases(update_connection, refresh_connection_schema, wait, conn_id, database, token, org_id):
    """What an admin does: edit the Database field, then refresh the schema."""
    update_connection(
        connection_id=conn_id,
        payload={"config": {"account": "acct", "warehouse": "WH", "database": database}},
        user_token=token, org_id=org_id,
    )
    refresh_connection_schema(connection_id=conn_id, user_token=token, org_id=org_id)
    run = wait(connection_id=conn_id, user_token=token, org_id=org_id)
    assert run and run["status"] == "completed", run


async def _agent_client(ds_id):
    async with async_session_maker() as db:
        ds = (await db.execute(
            select(DataSource).options(selectinload(DataSource.connections))
            .where(DataSource.id == ds_id))).scalar_one()
        clients = await DataSourceService().construct_clients(db, ds, None)
    return next(c for k, c in clients.items() if ":" in k)


def test_adding_and_removing_a_database_renames_tables_in_place(
    account, admin, test_client, create_connection, wait_for_connection_indexing, get_connection_tables,
    update_connection, refresh_connection_schema, create_domain_from_connection, update_tables_status_delta,
    get_data_source,
):
    token, org_id = admin
    conn = create_connection(
        name="Warehouse", type="snowflake",
        config={"account": "acct", "warehouse": "WH", "database": "DB_A"},
        credentials={"user": "svc", "password": "pw"},
        user_token=token, org_id=org_id,
    )
    run = wait_for_connection_indexing(connection_id=conn["id"], user_token=token, org_id=org_id)
    assert run and run["status"] == "completed", run

    before = _tables_by_name(get_connection_tables, conn["id"], token, org_id)
    assert set(before) == {"SALES.ORDERS", "SALES.SECRETS"}

    agent = create_domain_from_connection(name="Sales agent", connection_id=conn["id"], user_token=token, org_id=org_id)
    update_tables_status_delta(data_source_id=agent["id"], activate=["SALES.ORDERS"], deactivate=["SALES.SECRETS"],
                               user_token=token, org_id=org_id)

    # Add a second database.
    _set_databases(update_connection, refresh_connection_schema, wait_for_connection_indexing, conn["id"], "DB_A, DB_B", token, org_id)
    after = _tables_by_name(get_connection_tables, conn["id"], token, org_id)
    assert set(after) == {"DB_A.SALES.ORDERS", "DB_A.SALES.SECRETS", "DB_B.CRM.CUSTOMERS", "DB_B.SALES.ORDERS"}
    # Renamed in place, not replaced.
    assert after["DB_A.SALES.ORDERS"]["id"] == before["SALES.ORDERS"]["id"]
    assert after["DB_A.SALES.SECRETS"]["id"] == before["SALES.SECRETS"]["id"]

    agent_tables = _agent_tables(get_data_source, agent["id"], token, org_id, test_client)
    assert agent_tables["DB_A.SALES.ORDERS"]["is_active"] is True
    assert agent_tables["DB_A.SALES.SECRETS"]["is_active"] is False
    assert not any(n.count(".") == 1 for n in agent_tables), sorted(agent_tables)

    # Enable a table of the second database and check what the agent can read.
    update_tables_status_delta(data_source_id=agent["id"], activate=["DB_B.CRM.CUSTOMERS"],
                               user_token=token, org_id=org_id)
    client = asyncio.run(_agent_client(agent["id"]))
    enforce_table_scope(client, "SELECT * FROM DB_A.SALES.ORDERS o JOIN DB_B.CRM.CUSTOMERS c ON c.ID = o.CUSTOMER_ID")
    enforce_table_scope(client, "SELECT * FROM SALES.ORDERS")  # short name = default database
    for refused in ("SELECT * FROM DB_B.SALES.ORDERS", "SELECT * FROM DB_A.SALES.SECRETS",
                    "SELECT * FROM DB_C.SALES.ORDERS"):
        with pytest.raises(TableScopeViolation):
            enforce_table_scope(client, refused)

    # Remove it again: short names come back on the same rows.
    _set_databases(update_connection, refresh_connection_schema, wait_for_connection_indexing, conn["id"], "DB_A", token, org_id)
    back = _tables_by_name(get_connection_tables, conn["id"], token, org_id)
    assert set(back) == {"SALES.ORDERS", "SALES.SECRETS"}
    assert back["SALES.ORDERS"]["id"] == before["SALES.ORDERS"]["id"]
    agent_tables = _agent_tables(get_data_source, agent["id"], token, org_id, test_client)
    assert agent_tables["SALES.ORDERS"]["is_active"] is True
    assert agent_tables["SALES.SECRETS"]["is_active"] is False


def test_connection_test_reports_a_listed_database_the_role_cannot_read(
    account, admin, create_connection, test_connection_connectivity, wait_for_connection_indexing,
):
    token, org_id = admin
    conn = create_connection(
        name="Warehouse", type="snowflake",
        config={"account": "acct", "warehouse": "WH", "database": "DB_A, DB_B"},
        credentials={"user": "svc", "password": "pw"},
        user_token=token, org_id=org_id,
    )
    wait_for_connection_indexing(connection_id=conn["id"], user_token=token, org_id=org_id)
    assert test_connection_connectivity(connection_id=conn["id"], user_token=token, org_id=org_id)["success"] is True

    # The role loses access to the second database.
    account.databases.pop("DB_B")
    result = test_connection_connectivity(connection_id=conn["id"], user_token=token, org_id=org_id)
    assert result["success"] is False
    assert "DB_B" in result["message"]


async def _seed_user_rows(conn_id, ds_id, user_id, names):
    # Direct writes: per-user visibility rows are only produced by a crawl under
    # a user's own Snowflake sign-in (OAuth), which no test route can drive.
    from app.models.connection_table import ConnectionTable
    from app.models.datasource_table import DataSourceTable
    from app.models.user_connection_overlay import UserConnectionTable
    from app.models.user_data_source_overlay import UserDataSourceTable

    async with async_session_maker() as db:
        for name in names:
            ct = (await db.execute(select(ConnectionTable).where(
                ConnectionTable.connection_id == conn_id, ConnectionTable.name == name))).scalar_one()
            dt = (await db.execute(select(DataSourceTable).where(
                DataSourceTable.datasource_id == ds_id, DataSourceTable.connection_table_id == ct.id))).scalar_one()
            db.add(UserConnectionTable(connection_id=conn_id, user_id=user_id, table_name=name, connection_table_id=ct.id))
            db.add(UserDataSourceTable(data_source_id=ds_id, user_id=user_id, connection_id=conn_id,
                                       table_name=name, data_source_table_id=dt.id))
        await db.commit()


async def _user_row_names(conn_id, user_id):
    from app.models.user_connection_overlay import UserConnectionTable
    from app.models.user_data_source_overlay import UserDataSourceTable

    async with async_session_maker() as db:
        conn_rows = (await db.execute(select(UserConnectionTable.table_name).where(
            UserConnectionTable.connection_id == conn_id, UserConnectionTable.user_id == user_id))).scalars().all()
        ds_rows = (await db.execute(select(UserDataSourceTable.table_name).where(
            UserDataSourceTable.connection_id == conn_id, UserDataSourceTable.user_id == user_id))).scalars().all()
    return sorted(conn_rows), sorted(ds_rows)


def test_per_user_visibility_rows_follow_the_rename(
    account, admin, whoami, create_connection, wait_for_connection_indexing, update_connection,
    refresh_connection_schema, create_domain_from_connection,
):
    token, org_id = admin
    user_id = whoami(token)["id"]
    conn = create_connection(
        name="Warehouse", type="snowflake",
        config={"account": "acct", "warehouse": "WH", "database": "DB_A"},
        credentials={"user": "svc", "password": "pw"},
        user_token=token, org_id=org_id,
    )
    wait_for_connection_indexing(connection_id=conn["id"], user_token=token, org_id=org_id)
    agent = create_domain_from_connection(name="Sales agent", connection_id=conn["id"], user_token=token, org_id=org_id)
    asyncio.run(_seed_user_rows(conn["id"], agent["id"], user_id, ["SALES.ORDERS", "SALES.SECRETS"]))

    _set_databases(update_connection, refresh_connection_schema, wait_for_connection_indexing,
                   conn["id"], "DB_A, DB_B", token, org_id)
    expected = ["DB_A.SALES.ORDERS", "DB_A.SALES.SECRETS"]
    assert asyncio.run(_user_row_names(conn["id"], user_id)) == (expected, expected)

    _set_databases(update_connection, refresh_connection_schema, wait_for_connection_indexing,
                   conn["id"], "DB_A", token, org_id)
    expected = ["SALES.ORDERS", "SALES.SECRETS"]
    assert asyncio.run(_user_row_names(conn["id"], user_id)) == (expected, expected)
