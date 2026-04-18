"""
E2E tests for Connection OAuth flow.

Tests the full OAuth sign-in flow using MockOAuthProvider from
tests/mocks/mock_oauth_provider.py. Verifies credential storage, overlay sync,
multi-provider support, token refresh, and re-sign-in behavior.
"""
import json
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, AsyncMock, MagicMock

import pytest
import httpx

from tests.mocks.mock_oauth_provider import MockOAuthProvider, patch_oauth_for_tests
from app.services.connection_oauth_service import auto_provision_connection_credentials


def _state_from_authorize(auth_resp) -> str:
    """Extract the signed state JWT from the authorization_url returned by /authorize.

    State is embedded in the URL as a query param (it's signed server-side and not
    stored in a cookie, so the callback can't be spoofed by modifying cookies).
    """
    url = auth_resp.json()["authorization_url"]
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.parse_qs(parsed.query)["state"][0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestOAuthAuthorizeRoute:
    """Test GET /connections/{id}/oauth/authorize"""

    def test_authorize_returns_url(
        self, test_client, login_user, create_connection, create_user, whoami
    ):
        """Authorize endpoint returns an authorization_url with correct params."""
        user = create_user()
        token = login_user(user["email"], user["password"])
        me = whoami(token)
        org_id = me["organizations"][0]["id"]

        conn = create_connection(
            name="Test PowerBI",
            type="powerbi",
            config={},
            credentials={"tenant_id": "t1", "client_id": "c1", "client_secret": "s1"},
            auth_policy="user_required",
            allowed_user_auth_modes=["oauth"],
            user_token=token,
            org_id=org_id,
        )

        with patch_oauth_for_tests() as mock:
            response = test_client.get(
                f"/api/connections/{conn['id']}/oauth/authorize",
                headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
            )

        assert response.status_code == 200
        data = response.json()
        assert "authorization_url" in data
        url = data["authorization_url"]
        assert "mock-oauth.test/microsoft/authorize" in url
        assert "client_id=mock_ms_client_id" in url
        assert "code_challenge=" in url
        assert "state=" in url

        # State is a signed JWT embedded in the URL (not in a cookie — that would
        # be tamperable by the user, since cookies are client-controlled).
        # Only the PKCE verifier needs a cookie (per-session secret for the code exchange).
        assert "conn_oauth_verifier" in response.cookies
        assert "conn_oauth_state" not in response.cookies

    def test_authorize_nonexistent_connection(self, test_client, login_user, create_user, whoami):
        """Authorize returns 404 for non-existent connection."""
        user = create_user()
        token = login_user(user["email"], user["password"])
        me = whoami(token)
        org_id = me["organizations"][0]["id"]
        response = test_client.get(
            "/api/connections/nonexistent-id/oauth/authorize",
            headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
        )
        assert response.status_code == 404


class TestOAuthCallbackRoute:
    """Test GET /connections/oauth/callback"""

    def test_callback_stores_credentials(
        self, test_client, login_user, create_connection, create_user, whoami
    ):
        """Full flow: authorize → callback → credentials stored."""
        user = create_user()
        token = login_user(user["email"], user["password"])
        me = whoami(token)
        org_id = me["organizations"][0]["id"]

        conn = create_connection(
            name="Test PowerBI OAuth",
            type="powerbi",
            config={},
            credentials={"tenant_id": "t1", "client_id": "c1", "client_secret": "s1"},
            auth_policy="user_required",
            allowed_user_auth_modes=["oauth"],
            user_token=token,
            org_id=org_id,
        )

        with patch_oauth_for_tests() as mock:
            # Step 1: Get authorize URL (this sets the PKCE verifier cookie + returns signed state in URL)
            auth_resp = test_client.get(
                f"/api/connections/{conn['id']}/oauth/authorize",
                headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
            )
            assert auth_resp.status_code == 200
            state = _state_from_authorize(auth_resp)

            # Step 2: Simulate callback with the code and state
            callback_resp = test_client.get(
                f"/api/connections/oauth/callback?code=test_auth_code&state={state}",
                headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
                follow_redirects=False,
            )

            # Should redirect to /data?oauth=success
            assert callback_resp.status_code in (302, 307)
            assert "oauth=success" in callback_resp.headers.get("location", "")

        # Verify mock tracked the token exchange
        assert mock.total_tokens_issued == 1
        assert len(mock.exchange_log) == 1
        assert mock.exchange_log[0]["code"] == "test_auth_code"

    def test_callback_invalid_state(
        self, test_client, login_user, create_user, whoami
    ):
        """Callback rejects mismatched state."""
        user = create_user()
        token = login_user(user["email"], user["password"])
        me = whoami(token)
        org_id = me["organizations"][0]["id"]

        response = test_client.get(
            "/api/connections/oauth/callback?code=test_code&state=wrong_state",
            headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
        )
        assert response.status_code == 400

    def test_callback_missing_code(
        self, test_client, login_user, create_user, whoami
    ):
        """Callback rejects missing code."""
        user = create_user()
        token = login_user(user["email"], user["password"])
        me = whoami(token)
        org_id = me["organizations"][0]["id"]

        response = test_client.get(
            "/api/connections/oauth/callback?state=some_state",
            headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
        )
        assert response.status_code == 400


class TestOAuthMultiProvider:
    """Test that different connection types route to different OAuth providers."""

    def test_different_providers_different_urls(
        self, test_client, login_user, create_connection, create_user, whoami
    ):
        """PowerBI routes to Microsoft, BigQuery routes to Google."""
        user = create_user()
        token = login_user(user["email"], user["password"])
        me = whoami(token)
        org_id = me["organizations"][0]["id"]

        pbi_conn = create_connection(
            name="PowerBI",
            type="powerbi",
            config={},
            credentials={"tenant_id": "t1", "client_id": "c1", "client_secret": "s1"},
            auth_policy="user_required",
            allowed_user_auth_modes=["oauth"],
            user_token=token,
            org_id=org_id,
        )

        bq_conn = create_connection(
            name="BigQuery",
            type="bigquery",
            config={"project_id": "proj", "dataset": "ds"},
            credentials={"credentials_json": "{}", "oauth_client_id": "gc1", "oauth_client_secret": "gs1"},
            auth_policy="user_required",
            allowed_user_auth_modes=["oauth"],
            user_token=token,
            org_id=org_id,
        )

        with patch_oauth_for_tests() as mock:
            pbi_resp = test_client.get(
                f"/api/connections/{pbi_conn['id']}/oauth/authorize",
                headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
            )
            bq_resp = test_client.get(
                f"/api/connections/{bq_conn['id']}/oauth/authorize",
                headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
            )

        assert "mock-oauth.test/microsoft" in pbi_resp.json()["authorization_url"]
        assert "mock-oauth.test/google" in bq_resp.json()["authorization_url"]


class TestOAuthReSignIn:
    """Test that re-signing in upserts (not duplicates) credentials."""

    def test_re_signin_updates_existing(
        self, test_client, login_user, create_connection, create_user, whoami
    ):
        """Second OAuth sign-in for same user+connection updates existing row."""
        user = create_user()
        token = login_user(user["email"], user["password"])
        me = whoami(token)
        org_id = me["organizations"][0]["id"]

        conn = create_connection(
            name="PowerBI Re-signin",
            type="powerbi",
            config={},
            credentials={"tenant_id": "t1", "client_id": "c1", "client_secret": "s1"},
            auth_policy="user_required",
            allowed_user_auth_modes=["oauth"],
            user_token=token,
            org_id=org_id,
        )

        with patch_oauth_for_tests() as mock:
            # First sign-in
            auth_resp = test_client.get(
                f"/api/connections/{conn['id']}/oauth/authorize",
                headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
            )
            state1 = _state_from_authorize(auth_resp)
            test_client.get(
                f"/api/connections/oauth/callback?code=code1&state={state1}",
                headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
                follow_redirects=False,
            )

            # Second sign-in
            auth_resp2 = test_client.get(
                f"/api/connections/{conn['id']}/oauth/authorize",
                headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
            )
            state2 = _state_from_authorize(auth_resp2)
            test_client.get(
                f"/api/connections/oauth/callback?code=code2&state={state2}",
                headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
                follow_redirects=False,
            )

        # Both exchanges tracked, upsert worked (no unique constraint error)
        assert mock.total_tokens_issued == 2
        assert len(mock.exchange_log) == 2


class TestOAuthRegistryIntegration:
    """Test that the data source registry correctly exposes the oauth auth variant."""

    def test_fields_endpoint_includes_oauth(
        self, test_client, login_user, create_user, whoami
    ):
        """GET /data_sources/powerbi/fields should include oauth variant for user_required."""
        user = create_user()
        token = login_user(user["email"], user["password"])
        me = whoami(token)
        org_id = me["organizations"][0]["id"]

        response = test_client.get(
            "/api/data_sources/powerbi/fields?auth_policy=user_required",
            headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
        )
        assert response.status_code == 200
        data = response.json()

        # Check oauth is in the auth options
        auth_by_auth = data.get("auth", {}).get("by_auth", {})
        assert "oauth" in auth_by_auth
        assert auth_by_auth["oauth"]["title"] == "Sign in with Microsoft"

        # Check oauth has empty schema (no credential fields)
        creds_by_auth = data.get("credentials_by_auth", {})
        assert "oauth" in creds_by_auth
        oauth_schema = creds_by_auth["oauth"]
        # Empty schema means no properties or empty properties
        assert not oauth_schema.get("properties") or len(oauth_schema["properties"]) == 0

    def test_fields_endpoint_bigquery_oauth(
        self, test_client, login_user, create_user, whoami
    ):
        """BigQuery should show 'Sign in with Google' oauth variant."""
        user = create_user()
        token = login_user(user["email"], user["password"])
        me = whoami(token)
        org_id = me["organizations"][0]["id"]

        response = test_client.get(
            "/api/data_sources/bigquery/fields?auth_policy=user_required",
            headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
        )
        assert response.status_code == 200
        data = response.json()

        auth_by_auth = data.get("auth", {}).get("by_auth", {})
        assert "oauth" in auth_by_auth
        assert auth_by_auth["oauth"]["title"] == "Sign in with Google"


class TestOBOAutoProvision:
    """Test Phase 2: auto_provision_connection_credentials after Entra ID login."""

    @pytest.mark.asyncio
    async def test_auto_provision_creates_credentials(
        self, test_client, login_user, create_connection, create_user, whoami
    ):
        """Auto-provision creates UserConnectionCredentials for Entra-based connections."""
        user_data = create_user()
        token = login_user(user_data["email"], user_data["password"])
        me = whoami(token)
        org_id = me["organizations"][0]["id"]
        user_id = me["id"]

        # Create a user_required PowerBI connection
        conn = create_connection(
            name="AutoProv PowerBI",
            type="powerbi",
            config={},
            credentials={"tenant_id": "t1", "client_id": "c1", "client_secret": "s1"},
            auth_policy="user_required",
            allowed_user_auth_modes=["oauth"],
            user_token=token,
            org_id=org_id,
        )

        with patch_oauth_for_tests() as mock:
            # Simulate auto-provision (what happens after Entra login)
            from app.dependencies import async_session_maker
            from app.models.user import User
            from sqlalchemy import select

            async with async_session_maker() as db:
                user_obj = (await db.execute(select(User).where(User.id == user_id))).scalars().first()
                summary = await auto_provision_connection_credentials(
                    db, user_obj, "fake_entra_login_token"
                )

        assert len(summary["provisioned"]) == 1
        assert summary["provisioned"][0]["connection_id"] == conn["id"]
        assert summary["provisioned"][0]["type"] == "powerbi"
        assert len(summary["failed"]) == 0

        # Verify OBO exchange was called
        assert len(mock.obo_log) == 1
        assert mock.obo_log[0]["login_access_token"] == "fake_entra_login_token"
        assert mock.obo_log[0]["connection_id"] == conn["id"]

    @pytest.mark.asyncio
    async def test_auto_provision_skips_existing_valid_credentials(
        self, test_client, login_user, create_connection, create_user, whoami
    ):
        """Auto-provision skips connections where user already has valid credentials."""
        user_data = create_user()
        token = login_user(user_data["email"], user_data["password"])
        me = whoami(token)
        org_id = me["organizations"][0]["id"]
        user_id = me["id"]

        conn = create_connection(
            name="Already Connected",
            type="powerbi",
            config={},
            credentials={"tenant_id": "t1", "client_id": "c1", "client_secret": "s1"},
            auth_policy="user_required",
            allowed_user_auth_modes=["oauth"],
            user_token=token,
            org_id=org_id,
        )

        with patch_oauth_for_tests() as mock:
            from app.dependencies import async_session_maker
            from app.models.user import User
            from sqlalchemy import select

            async with async_session_maker() as db:
                user_obj = (await db.execute(select(User).where(User.id == user_id))).scalars().first()

                # First provision — creates credentials
                summary1 = await auto_provision_connection_credentials(
                    db, user_obj, "login_token_1"
                )
                assert len(summary1["provisioned"]) == 1

                # Second provision — should skip (credentials already exist and valid)
                summary2 = await auto_provision_connection_credentials(
                    db, user_obj, "login_token_2"
                )

        assert len(summary2["skipped"]) == 1
        assert summary2["skipped"][0]["reason"] == "valid_credentials_exist"
        assert len(summary2["provisioned"]) == 0
        # OBO only called once (first time)
        assert len(mock.obo_log) == 1

    @pytest.mark.asyncio
    async def test_auto_provision_skips_non_oauth_connections(
        self, test_client, login_user, create_connection, create_user, whoami
    ):
        """Auto-provision ignores connections that don't allow oauth auth mode."""
        user_data = create_user()
        token = login_user(user_data["email"], user_data["password"])
        me = whoami(token)
        org_id = me["organizations"][0]["id"]
        user_id = me["id"]

        # Connection with userpass only — no oauth
        create_connection(
            name="Userpass Only",
            type="powerbi",
            config={},
            credentials={"tenant_id": "t1", "client_id": "c1", "client_secret": "s1"},
            auth_policy="user_required",
            allowed_user_auth_modes=["userpass"],
            user_token=token,
            org_id=org_id,
        )

        with patch_oauth_for_tests() as mock:
            from app.dependencies import async_session_maker
            from app.models.user import User
            from sqlalchemy import select

            async with async_session_maker() as db:
                user_obj = (await db.execute(select(User).where(User.id == user_id))).scalars().first()
                summary = await auto_provision_connection_credentials(
                    db, user_obj, "login_token"
                )

        assert len(summary["provisioned"]) == 0
        assert len(mock.obo_log) == 0

    @pytest.mark.asyncio
    async def test_auto_provision_skips_bigquery(
        self, test_client, login_user, create_connection, create_user, whoami
    ):
        """Auto-provision does NOT provision BigQuery — OBO only works for Microsoft."""
        user_data = create_user()
        token = login_user(user_data["email"], user_data["password"])
        me = whoami(token)
        org_id = me["organizations"][0]["id"]
        user_id = me["id"]

        create_connection(
            name="BQ Connection",
            type="bigquery",
            config={"project_id": "proj", "dataset": "ds"},
            credentials={"credentials_json": "{}", "oauth_client_id": "gc1", "oauth_client_secret": "gs1"},
            auth_policy="user_required",
            allowed_user_auth_modes=["oauth"],
            user_token=token,
            org_id=org_id,
        )

        with patch_oauth_for_tests() as mock:
            from app.dependencies import async_session_maker
            from app.models.user import User
            from sqlalchemy import select

            async with async_session_maker() as db:
                user_obj = (await db.execute(select(User).where(User.id == user_id))).scalars().first()
                summary = await auto_provision_connection_credentials(
                    db, user_obj, "login_token"
                )

        # BigQuery is not in ENTRA_OBO_CONNECTION_TYPES, so it's not queried
        assert len(summary["provisioned"]) == 0
        assert len(mock.obo_log) == 0

    @pytest.mark.asyncio
    async def test_auto_provision_partial_failure(
        self, test_client, login_user, create_connection, create_user, whoami
    ):
        """If OBO fails for one connection, others still get provisioned."""
        user_data = create_user()
        token = login_user(user_data["email"], user_data["password"])
        me = whoami(token)
        org_id = me["organizations"][0]["id"]
        user_id = me["id"]

        pbi_conn = create_connection(
            name="PowerBI OK",
            type="powerbi",
            config={},
            credentials={"tenant_id": "t1", "client_id": "c1", "client_secret": "s1"},
            auth_policy="user_required",
            allowed_user_auth_modes=["oauth"],
            user_token=token,
            org_id=org_id,
        )

        fabric_conn = create_connection(
            name="Fabric Fail",
            type="ms_fabric",
            config={},
            credentials={"tenant_id": "t2", "client_id": "c2", "client_secret": "s2"},
            auth_policy="user_required",
            allowed_user_auth_modes=["oauth"],
            user_token=token,
            org_id=org_id,
        )

        mock = MockOAuthProvider()
        mock.set_obo_failure("ms_fabric", ValueError("AADSTS50013: Assertion failed"))

        with patch_oauth_for_tests(mock) as mock:
            from app.dependencies import async_session_maker
            from app.models.user import User
            from sqlalchemy import select

            async with async_session_maker() as db:
                user_obj = (await db.execute(select(User).where(User.id == user_id))).scalars().first()
                summary = await auto_provision_connection_credentials(
                    db, user_obj, "login_token"
                )

        # PowerBI succeeded, Fabric failed
        assert len(summary["provisioned"]) == 1
        assert summary["provisioned"][0]["type"] == "powerbi"
        assert len(summary["failed"]) == 1
        assert "AADSTS50013" in summary["failed"][0]["error"]

    @pytest.mark.asyncio
    async def test_auto_provision_multiple_connections(
        self, test_client, login_user, create_connection, create_user, whoami
    ):
        """Auto-provision handles multiple Entra connections in one pass."""
        user_data = create_user()
        token = login_user(user_data["email"], user_data["password"])
        me = whoami(token)
        org_id = me["organizations"][0]["id"]
        user_id = me["id"]

        pbi_conn = create_connection(
            name="PowerBI Multi",
            type="powerbi",
            config={},
            credentials={"tenant_id": "t1", "client_id": "c1", "client_secret": "s1"},
            auth_policy="user_required",
            allowed_user_auth_modes=["oauth"],
            user_token=token,
            org_id=org_id,
        )

        fabric_conn = create_connection(
            name="Fabric Multi",
            type="ms_fabric",
            config={},
            credentials={"tenant_id": "t2", "client_id": "c2", "client_secret": "s2"},
            auth_policy="user_required",
            allowed_user_auth_modes=["oauth"],
            user_token=token,
            org_id=org_id,
        )

        with patch_oauth_for_tests() as mock:
            from app.dependencies import async_session_maker
            from app.models.user import User
            from sqlalchemy import select

            async with async_session_maker() as db:
                user_obj = (await db.execute(select(User).where(User.id == user_id))).scalars().first()
                summary = await auto_provision_connection_credentials(
                    db, user_obj, "login_token"
                )

        assert len(summary["provisioned"]) == 2
        assert len(mock.obo_log) == 2
        provisioned_types = {c["type"] for c in summary["provisioned"]}
        assert provisioned_types == {"powerbi", "ms_fabric"}


class TestUserOverlaySync:
    """Tests for _upsert_user_overlay — per-user schema overlay stored on
    user_data_source_tables / user_data_source_columns.

    These tests mock the data source client to return different tables per
    invocation (mimicking a user gaining/losing access) and verify the overlay
    reflects the changes.
    """

    async def _provision_user_creds(self, db, user_id: str, connection_id: str, organization_id: str):
        """Directly insert a UserConnectionCredentials row with an access_token
        that embeds the user_id, so the mocked MsFabricClient.get_schemas can
        route per-user without going through the OBO flow (which returns opaque
        tokens). This lets the test focus on overlay behavior in isolation.
        """
        from app.models.user_connection_credentials import UserConnectionCredentials
        from datetime import datetime, timedelta, timezone
        cred = UserConnectionCredentials(
            connection_id=connection_id,
            user_id=user_id,
            organization_id=organization_id,
            auth_mode="oauth",
            is_active=True,
            is_primary=True,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        cred.encrypt_credentials({
            "access_token": f"token-for-user-{user_id}",
            "refresh_token": "refresh-x",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "token_type": "Bearer",
        })
        db.add(cred)
        await db.commit()

    @pytest.mark.asyncio
    async def test_overlay_differs_per_user_and_revokes_on_shrinkage(
        self, test_client, login_user, create_connection, create_user, whoami,
    ):
        """Two users with different data source permissions get different overlays;
        when a user loses access to a table, the overlay marks it revoked instead
        of leaving stale `is_accessible=True` rows."""
        from unittest.mock import patch as mock_patch
        from app.dependencies import async_session_maker
        from app.models.user import User
        from app.models.data_source import DataSource
        from app.models.user_data_source_overlay import UserDataSourceTable
        from app.services.data_source_service import DataSourceService
        from app.ai.prompt_formatters import Table, TableColumn
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        # Admin + second user in the same org (u1 is admin since they created the org)
        import uuid as _uuid
        u1_email = f"u1_{_uuid.uuid4().hex[:8]}@test.com"
        u2_email = f"u2_{_uuid.uuid4().hex[:8]}@test.com"
        u1 = create_user(email=u1_email)
        u1_token = login_user(u1["email"], u1["password"])
        me1 = whoami(u1_token)
        org_id = me1["organizations"][0]["id"]
        u1_id = me1["id"]
        # Admin invites u2 BEFORE u2 registers (required because uninvited signups disabled)
        invite_resp = test_client.post(
            f"/api/organizations/{org_id}/members",
            json={"email": u2_email, "role": "member", "organization_id": org_id},
            headers={"Authorization": f"Bearer {u1_token}", "X-Organization-Id": org_id},
        )
        assert invite_resp.status_code in (200, 201), invite_resp.text
        u2 = create_user(email=u2_email)
        u2_token = login_user(u2["email"], u2["password"])
        me2 = whoami(u2_token)
        u2_id = me2["id"]

        # Admin creates the Fabric connection + DataSource
        conn = create_connection(
            name="Overlay Fabric",
            type="ms_fabric",
            config={"server_hostname": "test.datawarehouse.fabric.microsoft.com", "database": "demo"},
            credentials={"tenant_id": "t1", "client_id": "c1", "client_secret": "s1"},
            auth_policy="user_required",
            allowed_user_auth_modes=["oauth"],
            user_token=u1_token,
            org_id=org_id,
        )
        ds_resp = test_client.post(
            "/api/data_sources",
            json={"name": "Overlay DS", "connection_id": conn["id"], "is_public": True, "generate_summary": False},
            headers={"Authorization": f"Bearer {u1_token}", "X-Organization-Id": org_id},
        )
        assert ds_resp.status_code == 200, ds_resp.text
        ds_id = ds_resp.json()["id"]

        sales = Table(
            name="dbo.sales",
            columns=[TableColumn(name="id", dtype="int"), TableColumn(name="region", dtype="varchar")],
            pks=[TableColumn(name="id", dtype="int")], fks=[], metadata_json=None,
        )
        finance = Table(
            name="dbo.finance",
            columns=[TableColumn(name="id", dtype="int"), TableColumn(name="budget", dtype="decimal")],
            pks=[TableColumn(name="id", dtype="int")], fks=[], metadata_json=None,
        )

        user1_tables = [sales, finance]
        user2_tables = [sales]

        def mock_get_schemas(self):
            tok = getattr(self, "_delegated_access_token", None) or ""
            if u1_id in tok:
                return list(user1_tables)
            if u2_id in tok:
                return list(user2_tables)
            return []

        # Provision user-scoped credentials directly so the mocked client can
        # route by the embedded user_id.
        async with async_session_maker() as db:
            await self._provision_user_creds(db, u1_id, conn["id"], org_id)
            await self._provision_user_creds(db, u2_id, conn["id"], org_id)

        svc = DataSourceService()
        with mock_patch(
            "app.data_sources.clients.ms_fabric_client.MsFabricClient.get_schemas",
            mock_get_schemas,
        ):
            async with async_session_maker() as db:
                ds = (await db.execute(
                    select(DataSource)
                    .options(selectinload(DataSource.connections))
                    .where(DataSource.id == ds_id)
                )).scalars().first()
                u1_obj = (await db.execute(select(User).where(User.id == u1_id))).scalars().first()
                u2_obj = (await db.execute(select(User).where(User.id == u2_id))).scalars().first()
                await svc.get_user_data_source_schema(db=db, data_source=ds, user=u1_obj)
                await svc.get_user_data_source_schema(db=db, data_source=ds, user=u2_obj)

            async with async_session_maker() as db:
                async def accessible_tables(uid):
                    r = await db.execute(
                        select(UserDataSourceTable.table_name)
                        .where(UserDataSourceTable.data_source_id == ds_id)
                        .where(UserDataSourceTable.user_id == uid)
                        .where(UserDataSourceTable.is_accessible == True)
                        .where(UserDataSourceTable.deleted_at.is_(None))
                        .order_by(UserDataSourceTable.table_name)
                    )
                    return [t for (t,) in r.all()]

                u1_tables_before = await accessible_tables(u1_id)
                u2_tables_before = await accessible_tables(u2_id)
                assert u1_tables_before == ["dbo.finance", "dbo.sales"], u1_tables_before
                assert u2_tables_before == ["dbo.sales"], u2_tables_before
                assert u1_tables_before != u2_tables_before

            # u1 loses access to dbo.finance upstream
            user1_tables.clear()
            user1_tables.append(sales)

            async with async_session_maker() as db:
                ds = (await db.execute(
                    select(DataSource).options(selectinload(DataSource.connections))
                    .where(DataSource.id == ds_id)
                )).scalars().first()
                u1_obj = (await db.execute(select(User).where(User.id == u1_id))).scalars().first()
                await svc.get_user_data_source_schema(db=db, data_source=ds, user=u1_obj)

            async with async_session_maker() as db:
                r = await db.execute(
                    select(UserDataSourceTable.table_name, UserDataSourceTable.is_accessible, UserDataSourceTable.status)
                    .where(UserDataSourceTable.data_source_id == ds_id)
                    .where(UserDataSourceTable.user_id == u1_id)
                    .where(UserDataSourceTable.deleted_at.is_(None))
                    .order_by(UserDataSourceTable.table_name)
                )
                by_name = {name: (acc, status) for (name, acc, status) in r.all()}
                assert by_name["dbo.sales"] == (True, "accessible"), by_name
                assert by_name["dbo.finance"] == (False, "revoked"), by_name

                # The LLM schema-context filter excludes the revoked row
                r = await db.execute(
                    select(UserDataSourceTable.table_name)
                    .where(UserDataSourceTable.data_source_id == ds_id)
                    .where(UserDataSourceTable.user_id == u1_id)
                    .where(UserDataSourceTable.is_accessible == True)
                    .where(UserDataSourceTable.deleted_at.is_(None))
                )
                assert [t for (t,) in r.all()] == ["dbo.sales"]

    @pytest.mark.asyncio
    async def test_overlay_resync_is_idempotent(
        self, test_client, login_user, create_connection, create_user, whoami,
    ):
        """Running the overlay sync twice with identical mock data should not
        duplicate rows or flip flags."""
        from unittest.mock import patch as mock_patch
        from app.dependencies import async_session_maker
        from app.models.user import User
        from app.models.data_source import DataSource
        from app.models.user_data_source_overlay import UserDataSourceTable, UserDataSourceColumn
        from app.services.data_source_service import DataSourceService
        from app.ai.prompt_formatters import Table, TableColumn
        from sqlalchemy import select, func
        from sqlalchemy.orm import selectinload

        u = create_user()
        token = login_user(u["email"], u["password"])
        me = whoami(token)
        org_id = me["organizations"][0]["id"]
        user_id = me["id"]

        conn = create_connection(
            name="Idempotent Fabric",
            type="ms_fabric",
            config={"server_hostname": "test.datawarehouse.fabric.microsoft.com", "database": "demo"},
            credentials={"tenant_id": "t1", "client_id": "c1", "client_secret": "s1"},
            auth_policy="user_required",
            allowed_user_auth_modes=["oauth"],
            user_token=token,
            org_id=org_id,
        )
        ds_resp = test_client.post(
            "/api/data_sources",
            json={"name": "Idempotent DS", "connection_id": conn["id"], "is_public": True, "generate_summary": False},
            headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
        )
        assert ds_resp.status_code == 200, ds_resp.text
        ds_id = ds_resp.json()["id"]

        sales = Table(
            name="dbo.sales",
            columns=[TableColumn(name="id", dtype="int"), TableColumn(name="region", dtype="varchar")],
            pks=[TableColumn(name="id", dtype="int")], fks=[], metadata_json=None,
        )

        def mock_get_schemas(self):
            return [sales]

        # Provision user-scoped credentials directly (same pattern as above).
        async with async_session_maker() as db:
            await self._provision_user_creds(db, user_id, conn["id"], org_id)

        svc = DataSourceService()
        with mock_patch(
            "app.data_sources.clients.ms_fabric_client.MsFabricClient.get_schemas",
            mock_get_schemas,
        ):
            async with async_session_maker() as db:
                ds = (await db.execute(
                    select(DataSource).options(selectinload(DataSource.connections))
                    .where(DataSource.id == ds_id)
                )).scalars().first()
                user_obj = (await db.execute(select(User).where(User.id == user_id))).scalars().first()
                await svc.get_user_data_source_schema(db=db, data_source=ds, user=user_obj)
                await svc.get_user_data_source_schema(db=db, data_source=ds, user=user_obj)

            async with async_session_maker() as db:
                t_count = (await db.execute(
                    select(func.count(UserDataSourceTable.id))
                    .where(UserDataSourceTable.data_source_id == ds_id)
                    .where(UserDataSourceTable.user_id == user_id)
                )).scalar()
                c_count = (await db.execute(
                    select(func.count(UserDataSourceColumn.id))
                    .join(UserDataSourceTable, UserDataSourceTable.id == UserDataSourceColumn.user_data_source_table_id)
                    .where(UserDataSourceTable.data_source_id == ds_id)
                    .where(UserDataSourceTable.user_id == user_id)
                )).scalar()
                assert t_count == 1, f"expected exactly 1 table row, got {t_count}"
                assert c_count == 2, f"expected exactly 2 column rows, got {c_count}"
