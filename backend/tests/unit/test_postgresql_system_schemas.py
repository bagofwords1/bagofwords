"""System catalog schemas are crawled only when a connection opts in."""
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from app.data_sources.clients.postgresql_client import PostgresqlClient
from app.schemas.data_sources.configs import PostgreSQLConfig

SYSTEM = ("pg_catalog", "information_schema")


def _crawl_statements(monkeypatch, client, fallback):
    """Return the SQL text the crawl sends to the database (driver mocked)."""
    conn = Mock()
    conn.execute.return_value.fetchall.return_value = []

    @contextmanager
    def connect():
        yield conn

    monkeypatch.setattr(client, "connect", connect)
    monkeypatch.setattr(client, "_attach_foreign_keys", Mock())
    if fallback:
        monkeypatch.setattr(
            client, "_get_tables_enriched", Mock(side_effect=RuntimeError("comments unavailable"))
        )
    client.get_schemas()
    return [str(call.args[0]) for call in conn.execute.call_args_list]


@pytest.mark.parametrize("fallback", [False, True])
def test_system_schemas_are_hidden_by_default(monkeypatch, fallback):
    client = PostgresqlClient("localhost", 5432, "demo", "demo")
    statements = _crawl_statements(monkeypatch, client, fallback)
    assert statements, "the crawl must query the catalog"
    # Every object-listing statement (tables and materialized views) must
    # filter the system schemas out, or they leak into the agent's catalog.
    for sql in statements:
        where = sql.split("WHERE", 1)[1]
        for name in SYSTEM:
            assert f"NOT IN" in where and name in where, sql


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("schema", [None, "pg_catalog", "public, pg_catalog"])
def test_opt_in_stops_filtering_system_schemas(monkeypatch, fallback, schema):
    client = PostgresqlClient(
        "localhost", 5432, "demo", "demo", schema=schema, include_system_schemas=True
    )
    statements = _crawl_statements(monkeypatch, client, fallback)
    assert statements
    for sql in statements:
        where = sql.split("WHERE", 1)[1]
        assert "NOT IN" not in where, sql
    if schema:
        # Narrowing to explicit schemas still works alongside the opt-in.
        assert all("IN (" in sql.split("WHERE", 1)[1] for sql in statements)


def test_config_exposes_opt_in_off_by_default():
    cfg = PostgreSQLConfig(host="h", database="d")
    assert cfg.include_system_schemas is False
    assert PostgreSQLConfig(host="h", database="d", include_system_schemas=True).include_system_schemas is True
    # The schema-driven connection form renders it as a toggle.
    field = PostgreSQLConfig.model_json_schema()["properties"]["include_system_schemas"]
    assert field["type"] == "boolean" and field["ui:type"] == "boolean"


def test_client_accepts_config_fields_as_kwargs():
    cfg = PostgreSQLConfig(host="h", database="d", include_system_schemas=True)
    client = PostgresqlClient(user="u", **cfg.model_dump())
    assert client.include_system_schemas is True
