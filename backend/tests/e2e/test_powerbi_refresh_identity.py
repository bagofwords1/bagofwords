"""Schema refresh repairs legacy namesakes without widening selected models."""

import asyncio
import threading
import time
import uuid
from unittest.mock import Mock

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session, selectinload

from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.connection_table import ConnectionTable
from app.models.data_source import DataSource
from app.models.datasource_table import DataSourceTable
from app.models.domain_connection import domain_connection
from app.models.user import User
from app.models.user_connection_credentials import UserConnectionCredentials
from app.models.user_data_source_overlay import UserDataSourceColumn, UserDataSourceTable
from app.services.connection_service import ConnectionService
from app.services.data_source_service import DataSourceService
from tests.unit.test_powerbi_schema_refresh import Tenant


def meta(ds):
    return {
        "powerbi": {
            "workspaceId": "ws-a" if ds == "ds-a" else "ws-b",
            "datasetId": ds,
            "workspaceName": "Workspace",
            "datasetName": "Analytics",
            "tableName": "Orders",
        }
    }


async def seed_legacy(org_id, user_id, wrong_link):
    # Deliberately construct persisted PRE-FIX state: the public API must no
    # longer be able to create an overlay whose model differs from its link.
    async with async_session_maker() as db:
        conn = Connection(
            name="Legacy " + uuid.uuid4().hex,
            type="powerbi",
            config={},
            organization_id=org_id,
            auth_policy="user_required",
        )
        conn.encrypt_credentials({"tenant_id": "t", "client_id": "c", "client_secret": "s"})
        db.add(conn)
        ds = DataSource(name="Agent " + uuid.uuid4().hex, organization_id=org_id, is_active=True, is_public=True)
        db.add(ds)
        await db.flush()
        await db.execute(domain_connection.insert().values(data_source_id=ds.id, connection_id=conn.id))
        ct = ConnectionTable(
            connection_id=conn.id,
            name="Analytics/Orders",
            columns=[{"name": "old", "dtype": "string"}],
            pks=[],
            fks=[],
            metadata_json=meta("ds-a"),
        )
        db.add(ct)
        await db.flush()
        dt = DataSourceTable(
            datasource_id=ds.id,
            connection_table_id=ct.id,
            name=ct.name,
            columns=ct.columns,
            pks=[],
            fks=[],
            metadata_json=ct.metadata_json,
            is_active=True,
        )
        db.add(dt)
        await db.flush()
        overlay = UserDataSourceTable(
            data_source_id=ds.id,
            user_id=user_id,
            connection_id=conn.id,
            table_name=ct.name,
            data_source_table_id=dt.id,
            metadata_json=meta("ds-b" if wrong_link else "ds-a"),
            is_accessible=True,
            status="accessible",
        )
        db.add(overlay)
        await db.flush()
        db.add(UserDataSourceColumn(user_data_source_table_id=overlay.id, column_name="old", is_accessible=True))
        cred = UserConnectionCredentials(
            connection_id=conn.id,
            user_id=user_id,
            organization_id=org_id,
            auth_mode="oauth",
            is_active=True,
            is_primary=True,
        )
        cred.encrypt_credentials({"access_token": "delegated-test-token"})
        db.add(cred)
        await db.commit()
        return conn.id, ds.id, dt.id


async def refresh_and_read(ids, user_id, shared=False, refresh=True, force_refresh=True):
    conn_id, ds_id, original_id = ids
    async with async_session_maker() as db:
        ds = (
            await db.execute(
                select(DataSource).options(selectinload(DataSource.connections)).where(DataSource.id == ds_id)
            )
        ).scalar_one()
        user = await db.get(User, user_id)
        svc = DataSourceService()
        if shared:
            conn = await db.get(Connection, conn_id)
            await ConnectionService().refresh_schema(db, conn)
            await svc.sync_domain_tables_from_connection(db, ds, conn)
        if refresh:
            await svc.get_user_data_source_schema(db, ds, user, force_refresh=force_refresh)
        await db.commit()
        rows = await svc.read_user_data_source_schema(db, ds, user)
        selected = await svc.read_user_data_source_schema(db, ds, user, active_only=True)
        canonical = (
            (await db.execute(select(DataSourceTable).where(DataSourceTable.datasource_id == ds_id))).scalars().all()
        )
        identities = {r.id: r.metadata_json["powerbi"]["datasetId"] for r in canonical}
        return rows, selected, identities


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("wrong_link", [False, True])
@pytest.mark.parametrize("shared", [False, True])
def test_refresh_preserves_model_identity_and_updates_columns(
    monkeypatch,
    create_user,
    login_user,
    whoami,
    wrong_link,
    shared,
    reverse,
):
    user = create_user(email=f"pbi-{uuid.uuid4().hex}@example.com")
    profile = whoami(login_user(user["email"], user["password"]))
    ids = asyncio.run(seed_legacy(profile["organizations"][0]["id"], profile["id"], wrong_link))
    models = [("ws-a", "ds-a", ["current_a"]), ("ws-b", "ds-b", ["current_b"])]
    tenant = Tenant(list(reversed(models)) if reverse else models)
    # Only the external HTTP transport is replaced; connector, services, and DB run real.
    monkeypatch.setattr("app.data_sources.clients.powerbi_client.requests.Session", lambda: tenant)
    token = Mock(status_code=200)
    token.json.return_value = {"access_token": "app-test-token"}
    monkeypatch.setattr("app.data_sources.clients.powerbi_client.requests.post", lambda *a, **k: token)
    rows, selected, canonical = asyncio.run(refresh_and_read(ids, profile["id"], shared))
    assert {t.metadata_json["powerbi"]["datasetId"] for t in rows} == {"ds-a", "ds-b"}
    assert len({t.name for t in rows}) == 2
    for t in rows:
        identity = t.metadata_json["powerbi"]["datasetId"]
        assert canonical[t.id] == identity
        assert {c.name for c in t.columns} == {"current_a" if identity == "ds-a" else "current_b"}
    assert {t.metadata_json["powerbi"]["datasetId"] for t in selected} == {"ds-a"}
    assert selected[0].id == ids[2]
    routine_rows, _, _ = asyncio.run(refresh_and_read(ids, profile["id"], force_refresh=False))
    assert {t.metadata_json["powerbi"]["datasetId"]: {c.name for c in t.columns} for t in routine_rows} == {
        "ds-a": {"current_a"},
        "ds-b": {"current_b"},
    }
    # Refresh is idempotent even when the identity sees only one of the namesakes later.
    tenant.models = [("ws-a", "ds-a", ["renamed_again"])]
    rows, selected, canonical = asyncio.run(refresh_and_read(ids, profile["id"]))
    assert len(rows) == 1
    assert {c.name for c in rows[0].columns} == {"renamed_again"}
    assert rows[0].id == ids[2]


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("unreadable", [False, True])
@pytest.mark.parametrize("role", ["admin", "member"])
def test_personal_refresh_endpoint_reads_current_columns(
    monkeypatch,
    test_client,
    create_user,
    login_user,
    whoami,
    role,
    unreadable,
    background,
):
    owner = create_user(email=f"owner-{uuid.uuid4().hex}@example.com")
    owner_token = login_user(owner["email"], owner["password"])
    profile = whoami(owner_token)
    org_id = profile["organizations"][0]["id"]
    token = owner_token
    if role == "member":
        email = f"member-{uuid.uuid4().hex}@example.com"
        invite = test_client.post(
            f"/api/organizations/{org_id}/members",
            json={"organization_id": org_id, "email": email, "role": "member"},
            headers={"Authorization": f"Bearer {owner_token}", "X-Organization-Id": org_id},
        )
        assert invite.status_code == 200, invite.text
        member = create_user(email=email)
        token = login_user(member["email"], member["password"])
        profile = whoami(token)
    ids = asyncio.run(seed_legacy(org_id, profile["id"], False))
    tenant = Tenant([("ws-a", "ds-a", ["renamed", "added"])])
    tenant.unreadable = unreadable
    monkeypatch.setattr("app.data_sources.clients.powerbi_client.requests.Session", lambda: tenant)
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}
    committed = threading.Event()

    def on_commit(session):
        committed.set()

    event.listen(Session, "after_commit", on_commit)
    try:
        response = test_client.post(
            f"/api/connections/{ids[0]}/my-schema/refresh?background={str(background).lower()}",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        diagnostics = response.json()
        if background:
            # Wait for actual DB commits, not arbitrary sleeps, and observe the
            # same public job status the client polls.
            deadline = time.monotonic() + 30
            while True:
                committed.clear()
                job_response = test_client.get(f"/api/connections/{ids[0]}/indexing?scope=user", headers=headers)
                assert job_response.status_code == 200
                job = job_response.json()
                if job["status"] in {"completed", "failed", "cancelled"}:
                    assert job["status"] == "completed", job
                    diagnostics = job["stats"]
                    break
                remaining = deadline - time.monotonic()
                assert remaining > 0 and committed.wait(remaining), "Background refresh did not finish"
    finally:
        event.remove(Session, "after_commit", on_commit)
    rows, selected, identities = asyncio.run(refresh_and_read(ids, profile["id"], refresh=False))
    if unreadable:
        assert rows == []
        assert diagnostics["unreadable_datasets"]
    else:
        assert {c.name for t in rows for c in t.columns} == {"renamed", "added"}
        assert selected[0].id == ids[2]


def test_shared_sync_does_not_merge_user_only_namesake(create_user, login_user, whoami):
    user = create_user(email=f"sync-{uuid.uuid4().hex}@example.com")
    profile = whoami(login_user(user["email"], user["password"]))
    ids = asyncio.run(seed_legacy(profile["organizations"][0]["id"], profile["id"], False))

    async def check():
        async with async_session_maker() as db:
            # Legacy namesake orphan: this pre-fix state cannot be created by
            # the corrected identity-aware discovery path.
            orphan = DataSourceTable(
                datasource_id=ids[1],
                name="Analytics/Orders",
                is_active=True,
                metadata_json={**meta("ds-b"), "discovered_connection_id": ids[0]},
                columns=[{"name": "private_b", "dtype": "string"}],
            )
            db.add(orphan)
            original = await db.get(DataSourceTable, ids[2])
            shared_table = await db.get(ConnectionTable, original.connection_table_id)
            shared_table.fks = [
                {
                    "column": {"name": "parent_id", "dtype": "int"},
                    "references_name": shared_table.name,
                    "references_column": {"name": "id", "dtype": "int"},
                }
            ]
            await db.commit()
            orphan_id = orphan.id
            ds = await db.get(DataSource, ids[1])
            conn = await db.get(Connection, ids[0])
            await DataSourceService().sync_domain_tables_from_connection(db, ds, conn)
            rows = (
                (await db.execute(select(DataSourceTable).where(DataSourceTable.datasource_id == ids[1])))
                .scalars()
                .all()
            )
            by_id = {r.id: r for r in rows}
            assert set(by_id) == {ids[2], orphan_id}
            assert by_id[orphan_id].connection_table_id is None
            assert by_id[orphan_id].metadata_json["powerbi"]["datasetId"] == "ds-b"
            assert by_id[ids[2]].metadata_json["powerbi"]["datasetId"] == "ds-a"
            assert len({r.name for r in rows}) == 2
            assert by_id[ids[2]].fks[0]["references_name"] == by_id[ids[2]].name
            assert all(r.is_active for r in rows)

    asyncio.run(check())
