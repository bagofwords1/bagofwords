"""A pooled Fabric connection must never cross delegated identities or rotations."""

import uuid

import pytest

from app.data_sources.clients.ms_fabric_client import MsFabricClient
from app.data_sources.engine_pool import dispose_for_uri


@pytest.mark.parametrize(
    "first,second",
    [
        ({"access_token": "shared-jwt-header-" * 8 + "alice"}, {"access_token": "shared-jwt-header-" * 8 + "bob"}),
        (
            {"tenant_id": "tenant", "client_id": "app", "client_secret": "old"},
            {"tenant_id": "tenant", "client_id": "app", "client_secret": "rotated"},
        ),
        (
            {"tenant_id": "tenant-a", "client_id": "app", "client_secret": "secret"},
            {"tenant_id": "tenant-b", "client_id": "app", "client_secret": "secret"},
        ),
    ],
)
def test_fabric_pool_reuses_only_identical_credentials(first, second):
    # Constructing SQLAlchemy engines never connects to Fabric. No service,
    # database, ODBC connection, or token validation is mocked here.
    host = f"{uuid.uuid4().hex}.fabric.example"
    params = {"server_hostname": host, "database": "demo"}
    a = MsFabricClient(**params, **first)
    same = MsFabricClient(**params, **first)
    b = MsFabricClient(**params, **second)
    try:
        assert a.extraction_engine is same.extraction_engine
        assert a.extraction_engine is not b.extraction_engine
    finally:
        dispose_for_uri(f"mssql+pyodbc://fabric/?odbc_connect={host}%2Fdemo")
