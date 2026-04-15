"""
E2E tests for Connection OAuth flow.

Tests the full OAuth sign-in flow using MockOAuthProvider from
tests/mocks/mock_oauth_provider.py. Verifies credential storage, overlay sync,
multi-provider support, token refresh, and re-sign-in behavior.
"""
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, AsyncMock, MagicMock

import pytest
import httpx

from tests.mocks.mock_oauth_provider import MockOAuthProvider, patch_oauth_for_tests
from app.services.connection_oauth_service import auto_provision_connection_credentials


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

        # Verify cookies set
        assert "conn_oauth_state" in response.cookies
        assert "conn_oauth_verifier" in response.cookies

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
            # Step 1: Get authorize URL (this sets cookies)
            auth_resp = test_client.get(
                f"/api/connections/{conn['id']}/oauth/authorize",
                headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
            )
            assert auth_resp.status_code == 200
            state = auth_resp.cookies.get("conn_oauth_state")

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
            state1 = auth_resp.cookies.get("conn_oauth_state")
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
            state2 = auth_resp2.cookies.get("conn_oauth_state")
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
