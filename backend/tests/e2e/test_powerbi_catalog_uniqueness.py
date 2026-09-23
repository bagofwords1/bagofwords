"""A catalog has one canonical row per connection/model/table, regardless of label."""
import asyncio
import threading
import uuid
from unittest.mock import Mock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.connection_table import ConnectionTable
from app.models.user import User
from app.services.connection_service import ConnectionService
from tests.e2e.test_powerbi_refresh_identity import meta, seed_legacy
from tests.unit.test_powerbi_schema_refresh import Tenant


@pytest.mark.parametrize("different_part", [None, "workspaceId", "datasetId", "tableName"])
def test_database_enforces_identity_independent_of_label(create_user, login_user, whoami, different_part):
    user = create_user(email=f"identity-{uuid.uuid4().hex}@example.com")
    profile = whoami(login_user(user["email"], user["password"]))
    ids = asyncio.run(seed_legacy(profile["organizations"][0]["id"], profile["id"], False))

    async def check():
        async with async_session_maker() as db:
            metadata = meta("ds-a")
            if different_part:
                metadata["powerbi"][different_part] = "another-" + uuid.uuid4().hex
            # Write directly to exercise the DB backstop, bypassing the service
            # that would normally reuse a matching identity instead of inserting.
            db.add(ConnectionTable(connection_id=ids[0], name="A different label",
                columns=[], pks=[], fks=[], metadata_json=metadata))
            if different_part is None:
                with pytest.raises(IntegrityError):
                    await db.flush()
                await db.rollback()
            else:
                await db.commit()
                rows = (await db.execute(select(ConnectionTable).where(ConnectionTable.connection_id == ids[0]))).scalars().all()
                assert len(rows) == 2
    asyncio.run(check())


@pytest.mark.parametrize("delegated", [False, True])
def test_concurrent_discovery_keeps_one_row_per_identity(monkeypatch, create_user, login_user, whoami, delegated):
    user = create_user(email=f"concurrent-{uuid.uuid4().hex}@example.com")
    profile = whoami(login_user(user["email"], user["password"]))
    ids = asyncio.run(seed_legacy(profile["organizations"][0]["id"], profile["id"], False))
    gate = threading.Barrier(2)

    class ConcurrentTenant(Tenant):
        def response(self, method, url, **kwargs):
            if url.endswith("/groups"):
                # Both calls have loaded their old catalog before discovery
                # completes, making the stale-read race deterministic.
                gate.wait(timeout=15)
            return super().response(method, url, **kwargs)

    monkeypatch.setattr("app.data_sources.clients.powerbi_client.requests.Session",
        lambda: ConcurrentTenant([("ws-a", "ds-a", ["current"]), ("ws-b", "ds-b", ["other"])]))
    token = Mock(status_code=200)
    token.json.return_value = {"access_token": "synthetic-token"}
    monkeypatch.setattr("app.data_sources.clients.powerbi_client.requests.post", lambda *a, **k: token)

    async def check():
        async def refresh():
            async with async_session_maker() as db:
                conn = await db.get(Connection, ids[0])
                current_user = await db.get(User, profile["id"]) if delegated else None
                await ConnectionService().refresh_schema(db, conn, current_user=current_user)
        await asyncio.gather(refresh(), refresh())
        async with async_session_maker() as db:
            rows = (await db.execute(select(ConnectionTable).where(ConnectionTable.connection_id == ids[0]))).scalars().all()
            assert len(rows) == 2
            assert {r.metadata_json["powerbi"]["datasetId"] for r in rows} == {"ds-a", "ds-b"}
            assert len({r.name for r in rows}) == 2
            original = next(r for r in rows if r.metadata_json["powerbi"]["datasetId"] == "ds-a")
            assert {c["name"] for c in original.columns} == ({"old"} if delegated else {"current"})
    asyncio.run(check())


def test_delegated_model_rename_reuses_shared_identity(monkeypatch, create_user, login_user, whoami):
    user = create_user(email=f"rename-{uuid.uuid4().hex}@example.com")
    profile = whoami(login_user(user["email"], user["password"]))
    ids = asyncio.run(seed_legacy(profile["organizations"][0]["id"], profile["id"], False))

    class RenamedTenant(Tenant):
        def response(self, method, url, **kwargs):
            response = super().response(method, url, **kwargs)
            if url.endswith("/datasets"):
                payload = response.json()
                for dataset in payload["value"]:
                    dataset["name"] = "Renamed model"
                response.json.return_value = payload
            return response

    monkeypatch.setattr("app.data_sources.clients.powerbi_client.requests.Session",
        lambda: RenamedTenant([("ws-a", "ds-a", ["personal_column"])]))

    async def check():
        async with async_session_maker() as db:
            conn = await db.get(Connection, ids[0])
            current_user = await db.get(User, profile["id"])
            for _ in range(2):
                await ConnectionService().refresh_schema(db, conn, current_user=current_user)
            rows = (await db.execute(select(ConnectionTable).where(ConnectionTable.connection_id == ids[0]))).scalars().all()
            assert len(rows) == 1
            # A personal crawl must neither insert another canonical identity
            # nor replace the shared identity's name/columns with personal data.
            assert rows[0].name == "Analytics/Orders"
            assert {c["name"] for c in rows[0].columns} == {"old"}
    asyncio.run(check())
