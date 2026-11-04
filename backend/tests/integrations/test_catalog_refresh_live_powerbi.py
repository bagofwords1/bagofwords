"""Opt-in, read-only Power BI verification; application writes use a throwaway DB.

PBI_TENANT_ID, PBI_CLIENT_ID and PBI_CLIENT_SECRET must be supplied in env.
No source queries or permissions are modified. Inject a transport failure only
in this process, after capturing a successful live catalog.
"""
import asyncio
import os

import pytest
import requests
from sqlalchemy import select

from app.data_sources.clients.discovery import IncompleteDiscoveryError
from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.datasource_table import DataSourceTable
from app.services.connection_service import ConnectionService
from tests.e2e.test_catalog_refresh_atomicity import seed, snapshot

pytestmark = pytest.mark.e2e


def test_live_powerbi_refresh_retains_selections_on_partial_discovery(
    monkeypatch, create_user, login_user, whoami,
):
    credentials = {key: os.environ.get(env) for key, env in {
        "tenant_id": "PBI_TENANT_ID", "client_id": "PBI_CLIENT_ID", "client_secret": "PBI_CLIENT_SECRET",
    }.items()}
    if not all(credentials.values()):
        pytest.skip("PBI credentials not supplied")
    if os.environ.get("PBI_ACCESS_TOKEN"):
        credentials = {"access_token": os.environ["PBI_ACCESS_TOKEN"]}
    user = create_user()
    org = whoami(login_user(user["email"], user["password"]))["organizations"][0]["id"]
    cid = asyncio.run(seed(org))

    async def baseline():
        async with async_session_maker() as db:
            c = await db.get(Connection, cid)
            c.type = "powerbi"
            c.config = {"workspaces": os.environ.get("PBI_WORKSPACES", "")}
            c.encrypt_credentials(credentials)
            await db.commit()
            tables = await ConnectionService().refresh_schema(db, c)
            models = {t.metadata_json["powerbi"]["datasetId"] for t in tables}
            assert len(models) >= 2, "This loop needs at least two readable models to exercise a partial result"
            rows = (await db.execute(select(DataSourceTable).join(
                DataSourceTable.connection_table).where(
                DataSourceTable.connection_table.has(connection_id=cid)))).scalars().all()
            assert rows
            for row in rows:
                row.is_active = True
            await db.commit()
            return sorted(models)[0], len(tables)

    model, count = asyncio.run(baseline())
    before = asyncio.run(snapshot(cid))
    original_request = requests.Session.request
    injected = []

    def request(self, method, url, **kwargs):
        # Fail every introspection fallback for one model; leave another model
        # discoverable over the actual Power BI API.
        if f"/datasets/{model}/" in url:
            injected.append(url)
            raise requests.exceptions.Timeout("Injected catalog timeout")
        # Admin scan is optional: force the normal per-model fallback so the
        # injected timeout cannot be bypassed by a successful tenant scan.
        if "/admin/workspaces/" in url:
            raise requests.exceptions.Timeout("Injected admin scan fallback")
        return original_request(self, method, url, **kwargs)

    async def refresh():
        async with async_session_maker() as db:
            await ConnectionService().refresh_schema(db, await db.get(Connection, cid))

    with monkeypatch.context() as patch:
        patch.setattr(requests.Session, "request", request)
        with pytest.raises(IncompleteDiscoveryError):
            asyncio.run(refresh())
    assert injected, "No model introspection was intercepted"
    assert asyncio.run(snapshot(cid)) == before
    asyncio.run(refresh())
    assert asyncio.run(snapshot(cid))[1:] == before[1:]
    print(f"Live Power BI loop: {count} tables; injected model timeout retained catalog and selections; recovery retained row identities.")
