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
    try:
        return client.execute_query(sql).values.tolist()
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
    assert "region=eu-west-1" in secret and "url_style=path" in secret and "use_ssl=true" in secret


def test_azure_connection_string_is_not_readable_from_sql_and_the_secret_applies():
    client = DuckDBClient(uris="", connection_string=AZURE_CONNECTION)
    assert AZURE_KEY not in _readable_from_sql(client)
    assert _rows(client, "SELECT name FROM which_secret('az://container/key.csv', 'azure')"), "Azure paths resolve to a secret"


def test_a_connection_without_keys_registers_no_secret():
    assert _rows(DuckDBClient(uris=""), "SELECT count(*) FROM duckdb_secrets()") == [[0]]
