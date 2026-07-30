"""
Unit tests for SnowflakeClient delegated OAuth support.

When an `access_token` is present (per-user "Sign in with Snowflake"), the
client must authenticate with authenticator=oauth + token and must NOT send a
`user` — Snowflake rejects logins where a provided user differs from the
token's subject. Password and keypair paths must be unaffected.
"""
from unittest.mock import MagicMock, patch

from app.data_sources.clients.snowflake_client import SnowflakeClient


def _build_engine_capture(client):
    """Build client.snowflake_engine with create_engine mocked; return its call."""
    with patch(
        "app.data_sources.clients.snowflake_client.sqlalchemy.create_engine",
        return_value=MagicMock(),
    ) as create_engine:
        _ = client.snowflake_engine
    assert create_engine.call_count == 1
    return create_engine.call_args


def test_oauth_token_uses_oauth_authenticator_and_omits_user():
    client = SnowflakeClient(
        account="acct",
        warehouse="WH",
        database="DB",
        schema="PUBLIC",
        access_token="tok-123",
    )
    args, kwargs = _build_engine_capture(client)

    url = str(args[0])
    assert "authenticator=oauth" in url
    # The token rides in connect_args, never the URL (long JWT + secret in repr).
    assert "tok-123" not in url
    assert kwargs["connect_args"] == {"token": "tok-123"}


def test_oauth_token_takes_precedence_over_stored_password():
    client = SnowflakeClient(
        account="acct",
        warehouse="WH",
        database="DB",
        schema="PUBLIC",
        user="SVC_USER",
        password="secret",
        access_token="tok-123",
    )
    args, kwargs = _build_engine_capture(client)

    url = str(args[0])
    assert "authenticator=oauth" in url
    assert "SVC_USER" not in url
    assert "secret" not in url
    assert kwargs["connect_args"] == {"token": "tok-123"}


def test_oauth_keeps_role_connect_arg():
    client = SnowflakeClient(
        account="acct",
        warehouse="WH",
        database="DB",
        schema="PUBLIC",
        role="ANALYST",
        access_token="tok-123",
    )
    args, _ = _build_engine_capture(client)
    assert "role=ANALYST" in str(args[0])


def test_password_auth_unchanged():
    client = SnowflakeClient(
        account="acct",
        warehouse="WH",
        database="DB",
        schema="PUBLIC",
        user="alice",
        password="pw",
    )
    args, kwargs = _build_engine_capture(client)

    url = str(args[0])
    assert "alice" in url
    assert "authenticator" not in url
    assert "connect_args" not in kwargs
