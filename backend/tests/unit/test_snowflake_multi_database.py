"""One Snowflake connection can cover several databases on the same account.

Contract:
  * `database` takes a comma-separated list; the first entry is the session's
    default database.
  * With ONE database nothing changes for existing connections: tables are
    named SCHEMA.TABLE.
  * With SEVERAL, every table is named DATABASE.SCHEMA.TABLE, read from its own
    database's INFORMATION_SCHEMA, so the same schema.table in two databases
    is two distinct tables.
  * `schema` entries written DB.SCHEMA limit that database; a plain SCHEMA
    belongs to the default database; a database with no entry is read in full.
  * The connection test names any listed database the role cannot read.
  * A table keeps a way back to the name it had in the other naming style, so
    adding or removing a database can rename stored tables in place.

Snowflake is faked at the driver boundary (tests/fixtures/fake_snowflake_catalog.py).
"""

import pytest

from app.data_sources.clients.snowflake_client import SnowflakeClient
from tests.fixtures.fake_snowflake_catalog import FakeSnowflakeAccount

ACCOUNT = {
    "DB_A": {"SALES": {"ORDERS": ["ID", "CUSTOMER_ID"], "SECRETS": ["ID"]}, "OPS": {"TICKETS": ["ID"]}},
    "DB_B": {"CRM": {"CUSTOMERS": ["ID", "NAME"]}, "SALES": {"ORDERS": ["ID"]}},
    "WAREHOUSE_X": {"PUBLIC": {"EVENTS": ["ID"]}},
}


@pytest.fixture
def account(monkeypatch):
    acct = FakeSnowflakeAccount(ACCOUNT)
    acct.install(monkeypatch)
    return acct


def _client(database, **kw):
    return SnowflakeClient("acct", "WH", database, user="svc", password="pw", **kw)


def _names(client):
    return sorted(t.name for t in client.get_schemas())


@pytest.mark.parametrize("database", ["DB_A", "db_a", "  DB_A  "])
def test_single_database_keeps_schema_table_names(account, database):
    tables = _client(database).get_schemas()

    assert sorted(t.name for t in tables) == ["OPS.TICKETS", "SALES.ORDERS", "SALES.SECRETS"]
    assert all(t.metadata_json["database"] == "DB_A" for t in tables)
    assert not account.reads_of("DB_B")


@pytest.mark.parametrize("database,expected_dbs", [
    ("DB_A, DB_B", ["DB_A", "DB_B"]),
    ("db_b,DB_A", ["DB_A", "DB_B"]),
    ("DB_A, DB_B, WAREHOUSE_X", ["DB_A", "DB_B", "WAREHOUSE_X"]),
])
def test_several_databases_name_every_table_with_its_database(account, database, expected_dbs):
    tables = _client(database).get_schemas()

    assert all(t.name.count(".") == 2 for t in tables)
    assert sorted({t.name.split(".")[0] for t in tables}) == expected_dbs
    for t in tables:
        db, schema, _ = t.name.split(".")
        assert t.metadata_json == {"schema": schema, "database": db}
    for db in expected_dbs:
        assert account.reads_of(db), f"{db} was never read"


def test_same_schema_and_table_in_two_databases_are_two_tables(account):
    names = _names(_client("DB_A, DB_B"))

    assert "DB_A.SALES.ORDERS" in names
    assert "DB_B.SALES.ORDERS" in names


def test_duplicate_databases_are_read_once(account):
    names = _names(_client("DB_A, db_a, DB_B"))

    assert len(names) == len(set(names))
    assert len(account.reads_of("DB_A")) == len(account.reads_of("DB_B"))


def test_schema_filter_routes_each_entry_to_its_database(account):
    names = _names(_client("DB_A, DB_B", schema="SALES, DB_B.CRM"))

    # Plain SALES -> default database only; DB_B limited to CRM.
    assert names == ["DB_A.SALES.ORDERS", "DB_A.SALES.SECRETS", "DB_B.CRM.CUSTOMERS"]


def test_database_without_a_schema_entry_is_read_in_full(account):
    names = _names(_client("DB_A, DB_B", schema="DB_A.OPS"))

    assert [n for n in names if n.startswith("DB_A.")] == ["DB_A.OPS.TICKETS"]
    assert {"DB_B.CRM.CUSTOMERS", "DB_B.SALES.ORDERS"} <= set(names)


def test_single_database_schema_filter_is_unchanged(account):
    assert _names(_client("DB_A", schema="sales")) == ["SALES.ORDERS", "SALES.SECRETS"]


@pytest.mark.parametrize("database,unreadable", [
    ("DB_A, DB_MISSING", "DB_MISSING"),
    ("DB_GONE, DB_B", "DB_GONE"),
])
def test_connection_test_names_a_database_the_role_cannot_read(account, database, unreadable):
    result = _client(database).test_connection()

    assert result["success"] is False
    assert unreadable in result["message"]


@pytest.mark.parametrize("database", ["DB_A", "DB_A, DB_B", "DB_B, WAREHOUSE_X"])
def test_connection_test_passes_when_every_database_is_readable(account, database):
    assert _client(database).test_connection()["success"] is True


def test_session_default_database_is_the_first_listed(account, monkeypatch):
    from app.data_sources.clients import snowflake_client

    seen = {}
    real_url = snowflake_client.URL
    monkeypatch.setattr(snowflake_client, "URL", lambda **kw: seen.update(kw) or real_url(**kw))
    _client("DB_B, DB_A").test_connection()

    assert seen["database"] == "DB_B"


@pytest.mark.parametrize("name", ["SALES.ORDERS", "OPS.TICKETS"])
def test_names_map_between_the_two_styles_through_the_default_database(name):
    single, multi = _client("DB_A"), _client("DB_A, DB_B")

    assert single.table_name_aliases(name) == [f"DB_A.{name}"]
    assert multi.table_name_aliases(f"DB_A.{name}") == [name]
    # A table of another database never had a two-part name.
    assert multi.table_name_aliases(f"DB_B.{name}") == []


def test_agent_is_told_tables_can_be_joined_only_with_several_databases():
    multi = _client("DB_A, DB_B").description
    single = _client("DB_A").description

    assert "DB_A" in multi and "DB_B" in multi and "DATABASE.SCHEMA.TABLE" in multi
    assert "DATABASE.SCHEMA.TABLE" not in single


def test_table_scope_hints_follow_the_database_count():
    assert _client("DB_A").table_scope_default_database == "DB_A"
    assert _client("db_b, DB_A").table_scope_default_database == "DB_B"
    assert _client("DB_A").table_scope_enforce_databases is False
    assert _client("DB_A, DB_B").table_scope_enforce_databases is True


# Shape the snowflake-sqlalchemy dialect returns from get_multi_foreign_keys
# (captured against a Snowflake emulator): keyed by the schema it was asked for,
# table and column names lower-cased, the target's schema without a database.
def _reflected_fks(schema=None, **_):
    return {
        "DB_A.SALES": {("DB_A.SALES", "order_items"): [{
            "constrained_columns": ["order_id"], "referred_schema": "sales",
            "referred_table": "orders", "referred_columns": ["id"], "name": "fk_items_order",
        }]},
        "DB_B.CRM": {("DB_B.CRM", "contacts"): [{
            "constrained_columns": ["customer_id"], "referred_schema": None,
            "referred_table": "customers", "referred_columns": ["id"], "name": "fk_contact_customer",
        }]},
    }.get(schema, {})


@pytest.mark.parametrize("database,edges", [
    ("DB_A", {"SALES.ORDER_ITEMS": ("ORDER_ID", "SALES.ORDERS", "ID")}),
    ("DB_A, DB_B", {
        "DB_A.SALES.ORDER_ITEMS": ("ORDER_ID", "DB_A.SALES.ORDERS", "ID"),
        "DB_B.CRM.CONTACTS": ("CUSTOMER_ID", "DB_B.CRM.CUSTOMERS", "ID"),
    }),
])
def test_declared_foreign_keys_attach_in_each_database(monkeypatch, database, edges):
    from unittest.mock import MagicMock

    FakeSnowflakeAccount({
        "DB_A": {"SALES": {"ORDERS": ["ID"], "ORDER_ITEMS": ["ORDER_ID"]}},
        "DB_B": {"CRM": {"CUSTOMERS": ["ID"], "CONTACTS": ["CUSTOMER_ID"]}},
    }).install(monkeypatch)
    inspector = MagicMock()
    inspector.get_multi_foreign_keys.side_effect = _reflected_fks
    monkeypatch.setattr("app.data_sources.fk_reflection.sqlalchemy.inspect", lambda conn: inspector)

    tables = {t.name: t for t in _client(database).get_schemas()}

    found = {
        name: (fk.column.name, fk.references_name, fk.references_column.name)
        for name, t in tables.items() for fk in (t.fks or [])
    }
    assert found == edges
    # Every edge points at a table that was indexed.
    assert all(target in tables for _, target, _ in found.values())
