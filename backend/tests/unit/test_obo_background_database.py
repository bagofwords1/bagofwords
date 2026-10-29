"""Background OBO must persist credentials/catalogs independently of request DB IO."""

import asyncio
import logging
import uuid

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import main  # noqa: F401 — register ORM mappers
from app import dependencies
from app.models.connection import Connection
from app.models.data_source import DataSource
from app.models.domain_connection import domain_connection
from app.models.membership import Membership
from app.models.organization import Organization
from app.models.user import User
from app.models.user_connection_credentials import UserConnectionCredentials
from app.services.connection_indexing_service import _get_background_loop, shutdown_background_loop
from app.services.connection_oauth_service import schedule_auto_provision

pytestmark = pytest.mark.db  # opens real sessions — see tests/unit/conftest.py



@pytest.mark.parametrize("connection_count", [1, 2])
def test_background_login_provisions_credentials_and_catalogs(monkeypatch, connection_count):
    from app.ai.prompt_formatters import Table, TableColumn
    from app.data_sources.clients.ms_fabric_client import MsFabricClient
    from app.models.user_data_source_overlay import UserDataSourceTable

    async def token_response(self, url, **kwargs):
        return httpx.Response(
            200,
            json={
                "access_token": "test-delegated-token",
                "refresh_token": "test-refresh",
                "expires_in": 3600,
            },
        )

    async def schemas(self, **kwargs):
        assert self._delegated_access_token == "test-delegated-token"
        return [Table(name=f"{self.database}.sales", columns=[TableColumn(name="id", dtype="int")], pks=[], fks=[])]

    monkeypatch.setattr(httpx.AsyncClient, "post", token_response)
    monkeypatch.setattr(MsFabricClient, "aget_schemas", schemas)
    logging.getLogger("app.services.connection_oauth_service").disabled = False
    logging.getLogger("sqlalchemy.pool").disabled = False
    shutdown_background_loop()

    async def scenario():
        import os

        url = (
            os.environ["TEST_DATABASE_URL"]
            .replace("postgresql://", "postgresql+asyncpg://")
            .replace("sqlite://", "sqlite+aiosqlite://")
        )
        # Test defaults use NullPool and mask this production failure. Warm a
        # real pool on the request loop before dispatching the public scheduler.
        engine = create_async_engine(url, pool_size=1, max_overflow=0)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(dependencies, "async_session_maker", maker)
        try:
            async with maker() as db:
                # Service-level seed: no HTTP login is needed to exercise the
                # scheduler contract; tokens and the Fabric driver are boundaries.
                org = Organization(name=f"obo-{uuid.uuid4().hex}")
                user = User(
                    name="Delegated user",
                    email=f"{uuid.uuid4().hex}@example.com",
                    hashed_password="unused",
                    is_active=True,
                )
                db.add_all([org, user])
                await db.flush()
                # Login provisioning now rechecks org membership before saving.
                db.add(Membership(user_id=user.id, organization_id=org.id, role="member"))
                ds = DataSource(name="delegated catalog", organization_id=org.id, owner_user_id=user.id)
                db.add(ds)
                await db.flush()
                ids = []
                for n in range(connection_count):
                    conn = Connection(
                        name=f"fabric-{n}",
                        type="ms_fabric",
                        organization_id=org.id,
                        config={"server_hostname": "test.invalid", "database": f"warehouse{n}"},
                        auth_policy="user_required",
                        allowed_user_auth_modes=["oauth"],
                    )
                    conn.encrypt_credentials(
                        {"tenant_id": "test-tenant", "client_id": "test-client", "client_secret": "test-secret"}
                    )
                    db.add(conn)
                    await db.flush()
                    ids.append(str(conn.id))
                    await db.execute(domain_connection.insert().values(data_source_id=ds.id, connection_id=conn.id))
                await db.commit()
                user_id = str(user.id)

            schedule_auto_provision(user_id, "test-login-assertion")

            async def drain():
                tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
                if tasks:
                    await asyncio.gather(*tasks)

            barrier = asyncio.run_coroutine_threadsafe(drain(), _get_background_loop())
            await asyncio.wait_for(asyncio.wrap_future(barrier), timeout=30)

            async with maker() as db:
                rows = (
                    (
                        await db.execute(
                            select(UserConnectionCredentials).where(UserConnectionCredentials.user_id == user_id)
                        )
                    )
                    .scalars()
                    .all()
                )
                assert {str(r.connection_id) for r in rows} == set(ids)
                assert all(r.auth_mode == "oauth" and r.decrypt_credentials().get("refresh_token") for r in rows)
                overlays = (
                    (await db.execute(select(UserDataSourceTable).where(UserDataSourceTable.user_id == user_id)))
                    .scalars()
                    .all()
                )
                assert {str(r.connection_id) for r in overlays} == set(ids)
                assert await db.scalar(text("SELECT 1")) == 1
        finally:
            shutdown_background_loop()
            await engine.dispose()

    asyncio.run(scenario())
