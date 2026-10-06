"""Stored-login recovery costs one exchange per app, not per connection, and a
Disconnect is honoured by login provisioning as well as by recovery."""

import asyncio
import time
import uuid

import httpx
import pytest

import main  # noqa: F401
from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.membership import Membership
from app.models.oauth_account import OAuthAccount
from app.models.organization import Organization
from app.models.user import User
from app.models.user_connection_credentials import UserConnectionCredentials
from app.services.connection_indexing_service import _get_background_loop, shutdown_background_loop
from app.settings.bow_config import OIDCProvider
from app.settings.config import settings


def _fabric(org_id, n):
    conn = Connection(
        name=f"Fabric {n}",
        type="ms_fabric",
        organization_id=org_id,
        auth_policy="user_required",
        allowed_user_auth_modes=["oauth"],
        config={"server_hostname": "test.invalid", "database": f"db{n}"},
    )
    conn.encrypt_credentials({"tenant_id": "t", "client_id": "login-client", "client_secret": "s"})
    return conn


@pytest.fixture
def obo_calls(monkeypatch):
    calls = []

    async def post(self, url, **kwargs):
        calls.append(kwargs["data"]["grant_type"])
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"access_token": "delegated", "refresh_token": "r", "expires_in": 3600},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    return calls


def test_recovery_for_many_connections_is_one_job_and_one_exchange(monkeypatch, obo_calls):
    from app.services.obo_recovery_service import schedule_stored_login_recovery

    provider = f"entra-{uuid.uuid4().hex}"
    monkeypatch.setattr(settings.bow_config, "oidc_providers", [OIDCProvider(
        name=provider, enabled=True, issuer="https://login.microsoftonline.com/t/v2.0",
        client_id="login-client", client_secret="s", scopes=["openid", "api://login-client/access"],
    )])
    shutdown_background_loop()

    async def scenario():
        async with async_session_maker() as db:
            org = Organization(name=f"o-{uuid.uuid4().hex}")
            user = User(name="u", email=f"{uuid.uuid4().hex}@example.com", hashed_password="x", is_active=True)
            db.add_all([org, user])
            await db.flush()
            db.add(Membership(user_id=user.id, organization_id=org.id, role="member"))
            conns = [_fabric(org.id, n) for n in range(5)]
            db.add_all(conns)
            db.add(OAuthAccount(
                user_id=user.id, oauth_name=provider, account_id=str(uuid.uuid4()), account_email=user.email,
                access_token="assertion", refresh_token="refresh", expires_at=int(time.time()) + 3600,
            ))
            await db.commit()
            user_id = str(user.id)
            # A status read renders every connection of the agent.
            for c in conns:
                schedule_stored_login_recovery(user_id, c)

        async def drain():
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if tasks:
                await asyncio.gather(*tasks)

        await asyncio.wait_for(asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(drain(), _get_background_loop())), 30)

        async with async_session_maker() as db:
            from sqlalchemy import select
            rows = (await db.scalars(select(UserConnectionCredentials).where(
                UserConnectionCredentials.user_id == user_id,
                UserConnectionCredentials.is_active.is_(True)))).all()
        assert len(rows) == 5
        assert obo_calls.count("urn:ietf:params:oauth:grant-type:jwt-bearer") == 1

    try:
        asyncio.run(scenario())
    finally:
        shutdown_background_loop()


@pytest.mark.asyncio
async def test_login_provisioning_does_not_undo_a_disconnect(obo_calls):
    from app.services.connection_oauth_service import auto_provision_connection_credentials
    from app.services.connection_service import ConnectionService
    from sqlalchemy import select

    async with async_session_maker() as db:
        org = Organization(name=f"o-{uuid.uuid4().hex}")
        user = User(name="u", email=f"{uuid.uuid4().hex}@example.com", hashed_password="x", is_active=True)
        db.add_all([org, user])
        await db.flush()
        db.add(Membership(user_id=user.id, organization_id=org.id, role="member"))
        conn = _fabric(org.id, 0)
        db.add(conn)
        await db.commit()

        await auto_provision_connection_credentials(db, user, "assertion")
        await ConnectionService().delete_user_credentials(db, str(conn.id), org, user)
        summary = await auto_provision_connection_credentials(db, user, "assertion")

        assert summary["provisioned"] == []
        active = (await db.scalars(select(UserConnectionCredentials).where(
            UserConnectionCredentials.user_id == str(user.id),
            UserConnectionCredentials.is_active.is_(True)))).all()
        assert active == []
