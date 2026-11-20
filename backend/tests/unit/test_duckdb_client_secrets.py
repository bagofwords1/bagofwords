"""Stored object-store keys are registered as DuckDB secrets, out of reach of SQL.

Runs real DuckDB through the client's public query surface and inspects
DuckDB's own state. Each query opens a fresh handle, configured as every
agent query is.
"""
import pytest

from app.data_sources.clients.duckdb_client import DuckDBClient

S3_SECRET = "s3-secret-marker-value"
S3_TOKEN = "s3-session-marker-value"
AZURE_KEY = "azure-account-key-marker"
AZURE_CONNECTION = (
    f"DefaultEndpointsProtocol=https;AccountName=acct;AccountKey={AZURE_KEY};EndpointSuffix=core.windows.net"
)


def _rows(client: DuckDBClient, sql: str) -> list:
    # Straight on the session the client builds: the query guard refuses
    # system-state reads before they reach DuckDB, and this checks DuckDB itself.
    try:
        with client.connect() as con:
            return [list(row) for row in con.execute(sql).fetchall()]
    except RuntimeError as e:
        if "httpfs" in str(e) or "azure" in str(e).lower() and "extension" in str(e).lower():
            pytest.skip(f"DuckDB extensions cannot be downloaded here: {e}")
        raise


def _readable_from_sql(client: DuckDBClient) -> str:
    found = [_rows(client, "SELECT secret_string FROM duckdb_secrets()")]
    for name in ("s3_secret_access_key", "s3_session_token", "azure_storage_connection_string"):
        found.append(_rows(client, f"SELECT current_setting('{name}')"))
    found.append(_rows(client, "SELECT name, value FROM duckdb_settings()"))
    return str(found)


def test_s3_keys_are_not_readable_from_sql_and_the_secret_applies():
    client = DuckDBClient(uris="", access_key="AKIDEXAMPLE", secret_key=S3_SECRET, region="eu-west-1", session_token=S3_TOKEN)
    readable = _readable_from_sql(client)
    assert S3_SECRET not in readable and S3_TOKEN not in readable
    assert _rows(client, "SELECT name FROM which_secret('s3://bucket/key.csv', 's3')"), "S3 paths resolve to a secret"
    secret = _rows(client, "SELECT secret_string FROM duckdb_secrets() WHERE type = 's3'")[0][0]
    assert "region=eu-west-1" in secret


def test_azure_connection_string_is_not_readable_from_sql_and_the_secret_applies():
    client = DuckDBClient(uris="", connection_string=AZURE_CONNECTION)
    assert AZURE_KEY not in _readable_from_sql(client)
    assert _rows(client, "SELECT name FROM which_secret('az://container/key.csv', 'azure')"), "Azure paths resolve to a secret"


def test_a_connection_without_keys_registers_no_secret():
    assert _rows(DuckDBClient(uris=""), "SELECT count(*) FROM duckdb_secrets()") == [[0]]


def test_the_unrestricted_file_setting_still_keeps_keys_out_of_sql(monkeypatch):
    """The escape hatch widens file access only; it does not put keys back in settings."""
    from app.settings.config import settings

    monkeypatch.setattr(settings.bow_config.security, "unsafe_allow_unrestricted_file_connectors", True)
    client = DuckDBClient(uris="", access_key="AKIDEXAMPLE", secret_key=S3_SECRET, session_token=S3_TOKEN,
                          connection_string=AZURE_CONNECTION)
    readable = _readable_from_sql(client)
    assert S3_SECRET not in readable and S3_TOKEN not in readable and AZURE_KEY not in readable
