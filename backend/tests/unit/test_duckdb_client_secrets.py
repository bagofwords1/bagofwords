"""Stored object-store keys are registered as DuckDB secrets, out of reach of SQL."""
import duckdb
import pytest

from app.data_sources.clients.duckdb_client import DuckDBClient

S3_SECRET = "s3-secret-marker-value"
S3_TOKEN = "s3-session-marker-value"
AZURE_KEY = "azure-account-key-marker"
AZURE_CONNECTION = (
    f"DefaultEndpointsProtocol=https;AccountName=acct;AccountKey={AZURE_KEY};EndpointSuffix=core.windows.net"
)


def _configured(client: DuckDBClient):
    con = duckdb.connect(":memory:")
    try:
        client._configure_httpfs(con)
    except (duckdb.IOException, duckdb.HTTPException) as e:
        pytest.skip(f"DuckDB extensions cannot be downloaded here: {e}")
    return con


def _readable_values(con) -> str:
    found = [con.execute("SELECT secret_string FROM duckdb_secrets()").fetchall()]
    for name in ("s3_secret_access_key", "s3_session_token", "azure_storage_connection_string"):
        found.append(con.execute(f"SELECT current_setting('{name}')").fetchall())
    found.append(con.execute("SELECT name, value FROM duckdb_settings()").fetchall())
    return str(found)


def test_s3_keys_are_not_readable_from_sql_and_the_secret_applies():
    client = DuckDBClient(uris="", access_key="AKIDEXAMPLE", secret_key=S3_SECRET, region="eu-west-1", session_token=S3_TOKEN)
    con = _configured(client)
    readable = _readable_values(con)
    assert S3_SECRET not in readable and S3_TOKEN not in readable
    applied = con.execute("SELECT name FROM which_secret('s3://bucket/key.csv', 's3')").fetchall()
    assert applied, "the S3 scope resolves to a registered secret"
    secret = con.execute("SELECT secret_string FROM duckdb_secrets()").fetchone()[0]
    assert "region=eu-west-1" in secret and "url_style=path" in secret and "use_ssl=true" in secret


def test_azure_connection_string_is_not_readable_from_sql_and_the_secret_applies():
    client = DuckDBClient(uris="", connection_string=AZURE_CONNECTION)
    con = _configured(client)
    assert AZURE_KEY not in _readable_values(con)
    assert con.execute("SELECT name FROM which_secret('az://container/key.csv', 'azure')").fetchall()
