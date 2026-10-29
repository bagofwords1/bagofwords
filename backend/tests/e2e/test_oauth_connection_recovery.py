"""Existing custom-app sessions recover data access before accepting a prompt."""
import asyncio
import time
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.data_source import DataSource
from app.models.domain_connection import domain_connection
from app.models.membership import Membership
from app.models.oauth_account import OAuthAccount
from app.models.user import User
from app.models.user_connection_credentials import UserConnectionCredentials
from app.services.connection_oauth_service import auto_provision_connection_credentials
from app.settings.bow_config import OIDCProvider
from app.settings.config import settings
from tests.e2e.test_oauth_app import APP_REDIRECT_URI, _issue_token


@pytest.mark.e2e
@pytest.mark.parametrize("role", ["admin", "member"])
@pytest.mark.parametrize("token_state", ["usable", "wrong_audience", "expired"])
def test_existing_custom_app_prompt_recovers_only_usable_stored_login(
    test_client, create_user, login_user, whoami, create_oauth_client, create_report, monkeypatch, role, token_state,
):
    from app.ai.prompt_formatters import Table, TableColumn
    from app.data_sources.clients.ms_fabric_client import MsFabricClient

    async def schemas(self, **kwargs):
        return [Table(name="probe", columns=[TableColumn(name="id", dtype="int")], pks=[], fks=[])]

    monkeypatch.setattr(MsFabricClient, "aget_schemas", schemas)
    provider = f"entra-{uuid.uuid4().hex}"
    monkeypatch.setattr(settings.bow_config, "oidc_providers", [OIDCProvider(
        name=provider, enabled=True, issuer="https://login.microsoftonline.com/tenant/v2.0",
        client_id="sso-app", client_secret="test-secret",
        scopes=["openid", "profile", "email", "api://fabric-app/customer-scope"],
    )])
    external_requests = []

    async def token_post(self, url, **kwargs):
        data = kwargs["data"]
        external_requests.append(data)
        assert data["grant_type"] != "refresh_token"  # no offline_access, no refresh token
        success = data.get("assertion") == "fresh-fabric-assertion"
        return httpx.Response(200 if success else 400, request=httpx.Request("POST", url), json=(
            {"access_token": "delegated-fabric", "expires_in": 3600} if success
            else {"error": "invalid_grant", "error_codes": [50013]}
        ))

    monkeypatch.setattr(httpx.AsyncClient, "post", token_post)
    person = create_user()
    login = login_user(person["email"], person["password"])
    me = whoami(login)
    org_id, user_id = me["organizations"][0]["id"], me["id"]
    client = create_oauth_client(user_token=login, org_id=org_id, scopes="app", trusted=True,
                                 redirect_uris=[APP_REDIRECT_URI])
    app_token = _issue_token(test_client, login_token=login, client=client, scope="app")["access_token"]

    report = create_report(user_token=app_token, org_id=org_id)

    async def seed_old_session():
        # This incident state cannot be created by a successful API login:
        # an earlier SSO callback stored a Graph token and failed provisioning.
        async with async_session_maker() as db:
            membership = await db.scalar(select(Membership).where(Membership.user_id == user_id))
            membership.role = role
            conn = Connection(name="Fabric", type="ms_fabric", organization_id=org_id,
                              auth_policy="user_required", allowed_user_auth_modes=["oauth"],
                              config={"server_hostname": "test.invalid", "database": "probe"})
            conn.encrypt_credentials({"tenant_id": "tenant", "client_id": "system-app",
                                      "oauth_client_id": "fabric-app", "client_secret": "test-secret"})
            ds = DataSource(name="Public Fabric agent", organization_id=org_id, owner_user_id=user_id, is_public=True)
            db.add_all([conn, ds])
            await db.flush()
            await db.execute(domain_connection.insert().values(data_source_id=ds.id, connection_id=conn.id))
            db.add(OAuthAccount(user_id=user_id, oauth_name=provider, account_id=str(uuid.uuid4()),
                               account_email=person["email"], access_token="fresh-fabric-assertion" if token_state == "usable" else "old-graph-assertion",
                               refresh_token=None, expires_at=int(time.time()) + (-60 if token_state == "expired" else 3600)))
            await db.commit()
            return str(conn.id), str(ds.id)

    conn_id, ds_id = asyncio.run(seed_old_session())
    response = test_client.post(f"/api/reports/{report['id']}/completions", json={
        "prompt": {"content": "Read my Fabric data", "mode": "chat"}, "stream": True,
    }, headers={"Authorization": f"Bearer {app_token}"})
    # No LLM is configured: retain the existing model-validation response.
    assert response.status_code == 400, response.text

    async def fresh_login_and_disconnect():
        from app.models.completion import Completion
        from app.models.organization import Organization
        from app.services.connection_service import ConnectionService
        from app.services.obo_recovery_service import recover_report_connections

        async with async_session_maker() as db:
            user, org = await db.get(User, user_id), await db.get(Organization, org_id)
            completions = list(await db.scalars(select(Completion).where(Completion.report_id == report["id"])))
            assert not completions
            rows = list(await db.scalars(select(UserConnectionCredentials).where(
                UserConnectionCredentials.user_id == user_id,
                UserConnectionCredentials.connection_id == conn_id)))
            assert len(rows) == (1 if token_state == "usable" else 0)
            if rows:
                assert rows[0].is_active
            result = await auto_provision_connection_credentials(db, user, "fresh-fabric-assertion")
            await recover_report_connections(db, user, org, report["id"])
            await ConnectionService().delete_user_credentials(db, conn_id, org, user)
            # An ordinary prompt must not undo an explicit Disconnect.
            await recover_report_connections(db, user, org, report["id"])
            row = await db.scalar(select(UserConnectionCredentials).where(
                UserConnectionCredentials.user_id == user_id, UserConnectionCredentials.connection_id == conn_id))
            assert not row.is_active
            result = await auto_provision_connection_credentials(db, user, "fresh-fabric-assertion")
            assert result["provisioned"]
            await db.refresh(row)
            assert row.is_active
            assert not (row.metadata_json or {}).get("auto_recovery_disabled")
    asyncio.run(fresh_login_and_disconnect())
    assert external_requests
