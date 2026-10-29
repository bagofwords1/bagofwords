"""Already-signed-in users recover missing delegated connections, or stay disconnected."""

import asyncio
import logging
import time
import uuid

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

import main  # noqa: F401
from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.membership import Membership
from app.models.oauth_account import OAuthAccount
from app.models.organization import Organization
from app.models.user import User
from app.models.user_connection_credentials import UserConnectionCredentials
from app.services.connection_identity import build_token_identity_status
from app.services.connection_indexing_service import _get_background_loop, shutdown_background_loop
from app.settings.bow_config import OIDCProvider
from app.settings.config import settings

pytestmark = pytest.mark.db  # opens real sessions — see tests/unit/conftest.py



@pytest.mark.parametrize("role", ["admin", "member"])
@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "expired",
        "rejected",
        "refresh_rejected",
        "no_account",
        "no_membership",
        "disconnected",
        "service_preference",
        "wrong_client",
        "refresh_recovers",
        "separate_refresh",
        "timeout",
        "no_refresh",
        "malformed_response",
    ],
)
def test_missing_connection_recovers_from_stored_login_or_fails_closed(monkeypatch, role, case, caplog):
    from app.ai.prompt_formatters import Table, TableColumn
    from app.ai.tools.implementations.agent_focus_common import prepare_run_agents
    from app.data_sources.clients.ms_fabric_client import MsFabricClient
    from app.models.data_source import DataSource
    from app.models.domain_connection import domain_connection
    from app.models.report import Report
    from app.models.user_data_source_overlay import UserDataSourceTable
    from app.services.connection_service import ConnectionService

    async def schemas(self, **kwargs):
        assert self._delegated_access_token == "delegated-fabric-token"
        return [Table(name="sales", columns=[TableColumn(name="id", dtype="int")], pks=[], fks=[])]

    monkeypatch.setattr(MsFabricClient, "aget_schemas", schemas)
    provider = f"entra-{uuid.uuid4().hex}"
    monkeypatch.setattr(
        settings.bow_config,
        "oidc_providers",
        [
            OIDCProvider(
                name=provider,
                enabled=True,
                issuer="https://login.microsoftonline.com/test-tenant/v2.0",
                client_id="login-client",
                client_secret="private-client-secret",
                scopes=(["openid", "User.Read", "GroupMember.Read.All", "api://fabric-client/customer-scope"]
                        if case == "separate_refresh" else
                        ["openid", "profile", "email", "api://login-client/access", "offline_access"]),
            )
        ],
    )
    calls = []

    async def get(self, url, **kwargs):
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={"token_endpoint": "https://login.microsoftonline.com/test-tenant/oauth2/v2.0/token"},
        )

    async def post(self, url, **kwargs):
        data = kwargs["data"]
        calls.append(data["grant_type"])
        if case == "timeout":
            raise httpx.ReadTimeout("private-token-must-not-be-logged")
        if case == "malformed_response":
            return httpx.Response(200, json={"access_token": "", "expires_in": 3600})
        if data["grant_type"] == "refresh_token":
            if case == "separate_refresh":
                assert data["scope"] == "api://fabric-client/customer-scope"
                assert data["client_id"] == "login-client"
            else:
                assert "api://login-client/access" in data["scope"]
            rejected = case == "refresh_rejected"
            access = "renewed-login-assertion"
        else:
            assert data["assertion"] in {"stored-login-assertion", "renewed-login-assertion"}
            rejected = case == "rejected" or (
                case == "refresh_recovers" and data["assertion"] == "stored-login-assertion"
            )
            access = "delegated-fabric-token"
        if rejected:
            return httpx.Response(
                400,
                request=httpx.Request("POST", url),
                json={
                    "error": "invalid_grant",
                    "error_description": "private-token-must-not-be-logged",
                    "error_codes": [50076],
                },
            )
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"access_token": access, "refresh_token": "rotated-refresh-token", "expires_in": 3600},
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    for name in ("app.services.connection_oauth_service", "app.services.obo_recovery_service"):
        logging.getLogger(name).disabled = False
    shutdown_background_loop()

    async def scenario():
        # Service-level seed models an existing login left behind by a failed
        # background job; HTTP registration cannot manufacture that incident.
        async with async_session_maker() as db:
            org = Organization(name=f"recover-{uuid.uuid4().hex}")
            user = User(
                name="Existing user", email=f"{uuid.uuid4().hex}@example.com", hashed_password="unused", is_active=True
            )
            db.add_all([org, user])
            await db.flush()
            if case != "no_membership":
                db.add(Membership(user_id=user.id, organization_id=org.id, role=role))
            conn = Connection(
                name="Fabric",
                type="ms_fabric",
                organization_id=org.id,
                auth_policy="user_required",
                allowed_user_auth_modes=["oauth"],
                config={"server_hostname": "test.invalid", "database": "sample"},
            )
            conn.encrypt_credentials(
                {
                    "tenant_id": "test-tenant",
                    "client_id": ("different-client" if case == "wrong_client" else
                                  "fabric-client" if case == "separate_refresh" else "login-client"),
                    "client_secret": "private-client-secret",
                }
            )
            db.add(conn)
            await db.flush()
            ds = DataSource(name="Recoverable catalog", organization_id=org.id, owner_user_id=user.id, is_public=True)
            db.add(ds)
            await db.flush()
            await db.execute(domain_connection.insert().values(data_source_id=ds.id, connection_id=conn.id))
            if case != "no_account":
                db.add(
                    OAuthAccount(
                        user_id=user.id,
                        oauth_name=provider,
                        account_id=str(uuid.uuid4()),
                        account_email=user.email,
                        access_token="stored-login-assertion",
                        refresh_token=None if case == "no_refresh" else "stored-refresh-token",
                        expires_at=int(time.time())
                        + (-60 if case in {"expired", "refresh_rejected", "no_refresh", "separate_refresh"} else 3600),
                    )
                )
            if case in {"disconnected", "service_preference"}:
                row = UserConnectionCredentials(
                    user_id=user.id,
                    connection_id=conn.id,
                    organization_id=org.id,
                    auth_mode="service_account" if case == "service_preference" else "oauth",
                    is_active=True,
                    metadata_json={"query_identity": "service_account"} if case == "service_preference" else {},
                )
                row.encrypt_credentials({"access_token": "previous-fabric-token"} if case == "disconnected" else {})
                db.add(row)
            await db.commit()
            if case == "disconnected":
                await ConnectionService().delete_user_credentials(db, str(conn.id), org, user)
            user_id, conn_id = str(user.id), str(conn.id)
            if case == "valid":
                # The streaming completion service calls this exact public
                # preparation path before starting AgentV2.
                await db.refresh(ds, ["connections"])
                report = Report(organization_id=org.id, user_id=user.id, data_sources=[ds])
                _, clients = await prepare_run_agents(db, org, user, report)
                assert not clients
            else:
                await build_token_identity_status(db, conn, user)

        async def drain():
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if tasks:
                await asyncio.gather(*tasks)

        async def finish():
            await asyncio.wait_for(
                asyncio.wrap_future(asyncio.run_coroutine_threadsafe(drain(), _get_background_loop())), 30
            )

        await finish()
        async with async_session_maker() as db:
            conn = await db.get(Connection, conn_id)
            user = await db.get(User, user_id)
            status = await build_token_identity_status(db, conn, user)
            rows = (
                (
                    await db.execute(
                        select(UserConnectionCredentials).where(UserConnectionCredentials.user_id == user_id)
                    )
                )
                .scalars()
                .all()
            )
            recovered = [r for r in rows if r.is_active and r.auth_mode == "oauth"]
            overlays = (
                await db.scalars(select(UserDataSourceTable).where(UserDataSourceTable.user_id == user_id))
            ).all()
            if case in {"valid", "expired", "refresh_recovers", "separate_refresh"}:
                assert {str(r.connection_id) for r in overlays if r.is_accessible} == {conn_id}
                assert len(recovered) == 1
                assert recovered[0].decrypt_credentials()["access_token"] == "delegated-fabric-token"
                assert status.effective_auth == "user"
                service = ConnectionService()
                assert (await service.resolve_credentials(db, conn, user))["access_token"] == "delegated-fabric-token"
                assert service.last_credential_identity == "user"
            else:
                assert not recovered
                assert not [r for r in overlays if r.is_accessible]
                if case == "disconnected":
                    assert rows and all(not r.is_active and r.decrypt_credentials() == {} for r in rows)
                if case != "service_preference":
                    assert status.effective_auth == "none"
                    with pytest.raises(HTTPException) as denied:
                        await ConnectionService().resolve_credentials(db, conn, user)
                    assert denied.value.status_code == 403
            count = len(calls)
        await finish()
        # A second status read must not repeatedly exchange a rejected token.
        assert len(calls) == count
        if case in {"no_account", "no_membership", "disconnected", "service_preference", "wrong_client"}:
            assert not calls
        assert "private-token-must-not-be-logged" not in caplog.text
        assert "stored-login-assertion" not in caplog.text

    try:
        asyncio.run(scenario())
    finally:
        shutdown_background_loop()
