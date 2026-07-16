"""Phase 2: advertisement content and the client registry."""

from __future__ import annotations

import json

import pytest

from ..config import AgentConfig
from ..data_sources import construct_client, resolve_client_class
from ..data_sources.postgresql_client import PostgresqlClient
from ..tunnel import EdgeAgentTunnel


@pytest.fixture
def config() -> AgentConfig:
    return AgentConfig(
        org_id="cust-b",
        edge_agent_id="nyc-01",
        edge_agent_name="NYC Office",
        connections=[
            {
                "name": "prod-pg",
                "type": "postgresql",
                "label": "NYC Production",
                "config": {"host": "db.internal", "port": 5432, "database": "analytics"},
                "credentials": {"user": "bow_reader", "password": "hunter2"},
                "query_timeout_seconds": 120,
            },
            {
                "name": "warehouse",
                "type": "postgresql",
                "config": {"host": "wh.internal", "database": "wh"},
                "credentials": {"user": "u", "password": "p"},
            },
        ],
    )


def test_advertisement_carries_no_credentials(config):
    """The one thing this process exists to withhold.

    Asserted against the serialized bytes, not the dict: a nested secret would
    still be absent from `payload.keys()` while sitting in the JSON that
    actually goes on the wire.
    """
    payload = EdgeAgentTunnel(config).build_advertisement()
    wire = json.dumps(payload)

    for secret in ("hunter2", "bow_reader", "password", "credentials"):
        assert secret not in wire, f"{secret!r} reached the advertisement"


def test_advertisement_shape_matches_the_design(config):
    payload = EdgeAgentTunnel(config).build_advertisement()

    assert payload["edge_agent_id"] == "nyc-01"
    assert payload["edge_agent_name"] == "NYC Office"
    assert payload["index_timeout_seconds"] == 900
    assert [c["name"] for c in payload["connections"]] == ["prod-pg", "warehouse"]
    assert payload["connections"][0]["type"] == "postgresql"


def test_per_connection_timeout_overrides_the_agent_default(config):
    payload = EdgeAgentTunnel(config).build_advertisement()
    by_name = {c["name"]: c for c in payload["connections"]}

    assert by_name["prod-pg"]["query_timeout_seconds"] == 120     # explicit
    assert by_name["warehouse"]["query_timeout_seconds"] == 300   # inherited


def test_org_is_the_subject_not_a_payload_field(config):
    """Tenancy must be attestable.

    Core NATS gives a subscriber no publisher identity, so an org_id in the body
    is something any agent could forge. It belongs in the subject, which the
    broker refuses to let a publisher lie about.
    """
    payload = EdgeAgentTunnel(config).build_advertisement()

    assert "org_id" not in payload
    assert config.advertisement_subject == "tunnel.cust-b.advertisements"


def test_registry_resolves_postgresql():
    assert resolve_client_class("postgresql") is PostgresqlClient


def test_unknown_type_names_what_is_supported():
    with pytest.raises(ValueError, match="postgresql"):
        resolve_client_class("oracle")


def test_construct_client_narrows_to_the_constructor(config):
    """A connection may carry agent-side keys the client knows nothing about."""
    params = {**config.connections[0].client_params(), "not_a_client_arg": 1}
    client = construct_client("postgresql", params)

    assert isinstance(client, PostgresqlClient)
    assert client.host == "db.internal"
    assert client.user == "bow_reader"


def test_password_is_url_quoted():
    """An unquoted '@' or '/' silently corrupts the URI, and the resulting
    error names the host rather than the password."""
    client = construct_client(
        "postgresql",
        {"host": "h", "database": "d", "user": "u", "password": "p@ss/word"},
    )
    assert "p%40ss%2Fword" in client.pg_uri
    assert "p@ss/word" not in client.pg_uri
