"""Provisioning invariants across independent PostgreSQL sessions.

Direct fixtures represent a stored external login/missing credential state that
normal local authentication APIs cannot create. Only Microsoft HTTP is stubbed.
"""

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import select

import main  # noqa: F401
from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.membership import Membership
from app.models.organization import Organization
from app.models.user import User
from app.models.user_connection_credentials import UserConnectionCredentials
from app.services.connection_oauth_service import auto_provision_connection_credentials
from app.services.connection_service import ConnectionService


async def seed(role, count=1):
    async with async_session_maker() as db:
        org = Organization(name=str(uuid.uuid4()))
        user = User(name="test", email=f"{uuid.uuid4()}@example.com", hashed_password="x", is_active=True)
        db.add_all([org, user])
        await db.flush()
        db.add(Membership(user_id=user.id, organization_id=org.id, role=role))
        connections = []
        for i in range(count):
            conn = Connection(
                name=f"Power BI {i}",
                type="powerbi",
                organization_id=org.id,
                auth_policy="user_required",
                allowed_user_auth_modes=["oauth"],
                config={},
            )
            conn.encrypt_credentials({"tenant_id": "tenant", "client_id": "client", "client_secret": "synthetic"})
            db.add(conn)
            connections.append(conn)
        await db.commit()
        return str(user.id), str(org.id), [str(c.id) for c in connections]


async def provision(uid, missing=True):
    async with async_session_maker() as db:
        user = await db.get(User, uid)
        return await auto_provision_connection_credentials(
            db, user, "assertion", missing_only=missing, client_id="client"
        )


def response(url, ok=True):
    return httpx.Response(
        200 if ok else 400,
        request=httpx.Request("POST", url),
        json={"access_token": "delegated", "expires_in": 3600} if ok else {"error_codes": [50013]},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "member"])
@pytest.mark.parametrize("missing", [True, False])
@pytest.mark.parametrize("change", ["disconnect", "membership", "connect", "disabled"])
async def test_inflight_provisioning_preserves_user_eligibility(monkeypatch, role, change, missing):
    uid, oid, ids = await seed(role)
    entered, release = asyncio.Event(), asyncio.Event()

    async def post(self, url, **kwargs):
        entered.set()
        await release.wait()
        return response(url)

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    job = asyncio.create_task(provision(uid, missing=missing))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        async with async_session_maker() as db:
            if change == "disconnect":
                await asyncio.wait_for(
                    ConnectionService().delete_user_credentials(
                        db, ids[0], await db.get(Organization, oid), await db.get(User, uid)
                    ),
                    10,
                )
            elif change == "connect":
                from app.services.credential_coordination import save_oauth_credentials

                await save_oauth_credentials(
                    db, await db.get(Connection, ids[0]), await db.get(User, uid), {"access_token": "manual-choice"}
                )
            elif change == "disabled":
                conn = await db.get(Connection, ids[0])
                conn.is_active = False
                await db.commit()
            else:
                member = await db.scalar(select(Membership).where(Membership.user_id == uid))
                await db.delete(member)
                await db.commit()
        release.set()
        await asyncio.wait_for(job, 10)
        async with async_session_maker() as db:
            active = (
                await db.scalars(
                    select(UserConnectionCredentials).where(
                        UserConnectionCredentials.user_id == uid, UserConnectionCredentials.is_active.is_(True)
                    )
                )
            ).all()
        if change == "connect":
            assert len(active) == 1
            assert active[0].decrypt_credentials()["access_token"] == "manual-choice"
        else:
            assert active == []
    finally:
        release.set()
        await asyncio.gather(job, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("second_is_recovery", [True, False])
async def test_same_user_jobs_do_not_duplicate_while_other_users_progress(monkeypatch, second_is_recovery):
    uid, _, _ = await seed("member")
    other, _, _ = await seed("admin")
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def post(self, url, **kwargs):
        calls.append(kwargs["data"]["assertion"])
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return response(url)

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    first = asyncio.create_task(provision(uid))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        second = await asyncio.wait_for(provision(uid, second_is_recovery), 10)
        assert second["provisioned"] == []
        assert len(calls) == 1
        assert (await asyncio.wait_for(provision(other), 10))["provisioned"]
        release.set()
        await asyncio.wait_for(first, 10)
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_exchange_is_deduplicated_per_identity(monkeypatch):
    uid, _, _ = await seed("member", count=5)
    calls = []

    async def post(self, url, **kwargs):
        calls.append(url)
        return response(url, ok=False)

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    result = await provision(uid)
    assert len(result["failed"]) == 5
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "transport"])
async def test_provisioning_releases_database_lock_after_failure(monkeypatch, failure):
    from sqlalchemy import text

    uid, _, _ = await seed("member")
    entered, release = asyncio.Event(), asyncio.Event()

    async def post(self, url, **kwargs):
        entered.set()
        await release.wait()
        if failure == "transport":
            raise httpx.ReadTimeout("synthetic")
        return response(url)

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    job = asyncio.create_task(provision(uid))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        async with async_session_maker() as db:
            if db.bind.dialect.name == "postgresql":
                # Inspect the actual server lock, then contend from a distinct
                # physical connection. This proves cross-process semantics,
                # rather than merely a Python mutex in this test process.
                locks = (
                    await db.execute(
                        text(
                            "SELECT classid::bigint, objid::bigint FROM pg_locks "
                            "WHERE locktype='advisory' AND granted AND objsubid=1"
                        )
                    )
                ).all()
                assert len(locks) == 1
                key = (locks[0][0] << 32) | locks[0][1]
                if key >= 1 << 63:
                    key -= 1 << 64
                assert not await db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key})
        if failure == "cancel":
            job.cancel()
            with pytest.raises(asyncio.CancelledError):
                await job
        else:
            release.set()
            assert (await job)["failed"]

        async def success(self, url, **kwargs):
            return response(url)

        monkeypatch.setattr(httpx.AsyncClient, "post", success)
        assert (await asyncio.wait_for(provision(uid), 10))["provisioned"]
        async with async_session_maker() as db:
            if db.bind.dialect.name == "postgresql":
                assert await db.scalar(text("SELECT count(*) FROM pg_locks WHERE locktype='advisory'")) == 0
    finally:
        release.set()
        if not job.done():
            job.cancel()
        await asyncio.gather(job, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["disconnect", "new_connect"])
async def test_failed_manual_verification_does_not_undo_newer_choice(choice):
    from app.services.credential_coordination import restore_failed_oauth_credentials, save_oauth_credentials

    uid, oid, ids = await seed("member")
    async with async_session_maker() as first:
        user, conn = await first.get(User, uid), await first.get(Connection, ids[0])
        row, prior, blob = await save_oauth_credentials(first, conn, user, {"access_token": "old-attempt"})
        row_id = str(row.id)
        async with async_session_maker() as second:
            if choice == "disconnect":
                await ConnectionService().delete_user_credentials(
                    second, ids[0], await second.get(Organization, oid), await second.get(User, uid)
                )
            else:
                await save_oauth_credentials(
                    second,
                    await second.get(Connection, ids[0]),
                    await second.get(User, uid),
                    {"access_token": "new-attempt"},
                )
        await restore_failed_oauth_credentials(first, uid, row_id, blob, prior)
    async with async_session_maker() as db:
        rows = (
            await db.scalars(select(UserConnectionCredentials).where(UserConnectionCredentials.user_id == uid))
        ).all()
        assert len(rows) == 1
        if choice == "disconnect":
            assert not rows[0].is_active
            assert rows[0].metadata_json["auto_recovery_disabled"]
        else:
            assert rows[0].decrypt_credentials()["access_token"] == "new-attempt"
