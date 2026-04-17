"""
Integration tests for OAuth Delegated Credentials (Phase 1).

Tests the full OAuth delegated flow against real Entra ID + Fabric/PowerBI:

1. Device code flow to get user tokens (interactive — requires browser)
2. OBO exchange to get connection-scoped tokens
3. Fabric client connectivity with delegated tokens
4. Permission enforcement: demo1 sees all tables, demo2 sees only sales
5. get_oauth_params and authorize endpoint with real credentials

Usage:
    # Set required env vars before running (never commit these):
    export BOW_ENTRA_TENANT_ID='...'
    export BOW_ENTRA_CLIENT_ID='...'
    export BOW_ENTRA_CLIENT_SECRET='...'
    export BOW_OAUTH_TEST_DEMO1_EMAIL='...'
    export BOW_OAUTH_TEST_DEMO1_PASSWORD='...'
    export BOW_OAUTH_TEST_DEMO2_EMAIL='...'
    export BOW_OAUTH_TEST_DEMO2_PASSWORD='...'
    export BOW_FABRIC_SERVER='...'        # e.g. abc123.datawarehouse.fabric.microsoft.com

    # Non-interactive tests (client_credentials, params, authorize endpoint):
    pytest tests/integrations/test_oauth_delegated.py -v -s -k "not Interactive"

    # Interactive tests (requires browser login for device code):
    pytest tests/integrations/test_oauth_delegated.py -v -s -k "Interactive"

    # All tests:
    pytest tests/integrations/test_oauth_delegated.py -v -s

Requires pyodbc with "ODBC Driver 18 for SQL Server" for Fabric tests.
"""
import asyncio
import json
import os
import logging
import time
import pytest
import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Test configuration — all secrets come from env vars. Set these to your Entra
# app registration and demo user creds before running these tests. Never commit
# credentials to the repo.
# ---------------------------------------------------------------------------

TENANT_ID = os.environ.get("BOW_ENTRA_TENANT_ID", "")
CLIENT_ID = os.environ.get("BOW_ENTRA_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("BOW_ENTRA_CLIENT_SECRET", "")

# Demo users (set via env to avoid committing credentials)
DEMO1_EMAIL = os.environ.get("BOW_OAUTH_TEST_DEMO1_EMAIL", "")
DEMO1_PASSWORD = os.environ.get("BOW_OAUTH_TEST_DEMO1_PASSWORD", "")
DEMO2_EMAIL = os.environ.get("BOW_OAUTH_TEST_DEMO2_EMAIL", "")
DEMO2_PASSWORD = os.environ.get("BOW_OAUTH_TEST_DEMO2_PASSWORD", "")

# Fabric connection details
FABRIC_SERVER = os.environ.get("BOW_FABRIC_SERVER", "")
FABRIC_DATABASE = os.environ.get("BOW_FABRIC_DATABASE", "demo_db")

# Scopes
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
# For OBO to work, the login token must have aud=app's own client_id.
# This requires "Expose an API" with Application ID URI set on the app registration.
LOGIN_SCOPE = f"api://{CLIENT_ID}/access_as_user"
LOGIN_SCOPE_BASIC = "openid profile email"  # For non-OBO tests

# Cache for device code tokens (avoid re-authenticating for each test)
_token_cache = {}


def _skip_if_no_secret():
    missing = [
        name for name, val in [
            ("BOW_ENTRA_TENANT_ID", TENANT_ID),
            ("BOW_ENTRA_CLIENT_ID", CLIENT_ID),
            ("BOW_ENTRA_CLIENT_SECRET", CLIENT_SECRET),
            ("BOW_OAUTH_TEST_DEMO1_EMAIL", DEMO1_EMAIL),
            ("BOW_OAUTH_TEST_DEMO1_PASSWORD", DEMO1_PASSWORD),
            ("BOW_OAUTH_TEST_DEMO2_EMAIL", DEMO2_EMAIL),
            ("BOW_OAUTH_TEST_DEMO2_PASSWORD", DEMO2_PASSWORD),
        ] if not val
    ]
    if missing:
        pytest.skip(f"OAuth test env vars not set ({', '.join(missing)}) — skipping real OAuth tests")


# ---------------------------------------------------------------------------
# ROPC helper — get token for a user (non-interactive)
# ---------------------------------------------------------------------------

async def _ropc_login(username: str, password: str, scope: str = LOGIN_SCOPE) -> dict:
    """Get tokens via Resource Owner Password Credentials grant.

    ROPC must be enabled on the Entra app registration.
    """
    cache_key = f"ropc|{username}|{scope}"
    if cache_key in _token_cache:
        return _token_cache[cache_key]

    token_url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = {
        "grant_type": "password",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "username": username,
        "password": password,
        "scope": scope,
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            token_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )

    if resp.status_code >= 400:
        detail = resp.json()
        pytest.fail(
            f"ROPC login failed for {username}: {resp.status_code}\n"
            f"Error: {detail.get('error')}\n"
            f"Description: {detail.get('error_description')}"
        )

    _token_cache[cache_key] = resp.json()
    return resp.json()


# ---------------------------------------------------------------------------
# Device code flow — interactive user authentication (fallback)
# ---------------------------------------------------------------------------

async def _device_code_login(scope: str = LOGIN_SCOPE, label: str = "") -> dict:
    """Get tokens via device code flow. Requires user to open browser and authenticate.

    Caches tokens by (scope, label) to avoid re-authenticating per test.
    """
    cache_key = f"{scope}|{label}"
    if cache_key in _token_cache:
        return _token_cache[cache_key]

    device_url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/devicecode"
    token_url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"

    # Step 1: Request device code
    async with httpx.AsyncClient() as client:
        resp = await client.post(device_url, data={
            "client_id": CLIENT_ID,
            "scope": scope,
        }, timeout=30)

    if resp.status_code >= 400:
        pytest.fail(f"Device code request failed: {resp.json()}")

    device_data = resp.json()
    device_code = device_data["device_code"]
    interval = device_data.get("interval", 5)

    # Step 2: Show user the code and wait for authentication
    print(f"\n{'='*60}")
    print(f"  AUTHENTICATE {label or 'user'}")
    print(f"  Go to: {device_data['verification_uri']}")
    print(f"  Enter code: {device_data['user_code']}")
    print(f"{'='*60}\n")

    # Step 3: Poll for token
    for _ in range(60):  # 5 min timeout
        await asyncio.sleep(interval)
        async with httpx.AsyncClient() as client:
            resp = await client.post(token_url, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": CLIENT_ID,
                "device_code": device_code,
            }, timeout=30)

        data = resp.json()
        if resp.status_code == 200:
            print(f"  Authenticated {label or 'user'} successfully!")
            _token_cache[cache_key] = data
            return data

        error = data.get("error")
        if error == "authorization_pending":
            continue
        elif error == "slow_down":
            interval += 5
            continue
        else:
            pytest.fail(f"Device code auth failed: {data}")

    pytest.fail("Device code auth timed out after 5 minutes")


# ---------------------------------------------------------------------------
# OBO exchange helper
# ---------------------------------------------------------------------------

async def _obo_exchange(login_access_token: str, scope: str) -> dict:
    """Exchange login token for a resource-scoped token via OBO."""
    token_url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "assertion": login_access_token,
        "scope": scope,
        "requested_token_use": "on_behalf_of",
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            token_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )

    if resp.status_code >= 400:
        detail = resp.json()
        pytest.fail(
            f"OBO exchange failed: {resp.status_code}\n"
            f"Error: {detail.get('error')}\n"
            f"Description: {detail.get('error_description')}"
        )

    return resp.json()


# ===========================================================================
# Non-interactive tests (no browser needed)
# ===========================================================================

class TestClientCredentials:
    """Verify the app registration works with client_credentials grant."""

    @pytest.mark.asyncio
    async def test_client_credentials_graph(self):
        """App can get a token via client_credentials for Graph API."""
        _skip_if_no_secret()
        token_url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
        async with httpx.AsyncClient() as client:
            resp = await client.post(token_url, data={
                "grant_type": "client_credentials",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "scope": "https://graph.microsoft.com/.default",
            }, timeout=30)

        assert resp.status_code == 200
        data = resp.json()
        assert data["token_type"] == "Bearer"
        assert data["expires_in"] > 0
        logger.info(f"Client credentials OK — expires_in: {data['expires_in']}s")

    @pytest.mark.asyncio
    async def test_client_credentials_fabric_scope(self):
        """App can get a token for Fabric scope (service principal)."""
        _skip_if_no_secret()
        token_url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
        async with httpx.AsyncClient() as client:
            resp = await client.post(token_url, data={
                "grant_type": "client_credentials",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "scope": FABRIC_SCOPE,
            }, timeout=30)

        assert resp.status_code == 200
        logger.info("Client credentials for Fabric scope OK")

    @pytest.mark.asyncio
    async def test_client_credentials_powerbi_scope(self):
        """App can get a token for PowerBI scope (service principal)."""
        _skip_if_no_secret()
        token_url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
        async with httpx.AsyncClient() as client:
            resp = await client.post(token_url, data={
                "grant_type": "client_credentials",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "scope": POWERBI_SCOPE,
            }, timeout=30)

        assert resp.status_code == 200
        logger.info("Client credentials for PowerBI scope OK")


class TestGetOAuthParams:
    """Test get_oauth_params() with real connection credentials."""

    def test_powerbi_params(self):
        """get_oauth_params returns correct Entra URLs for PowerBI."""
        from unittest.mock import MagicMock
        from app.services.connection_oauth_service import get_oauth_params

        conn = MagicMock()
        conn.id = "test-pbi"
        conn.type = "powerbi"
        conn.decrypt_credentials.return_value = {
            "tenant_id": TENANT_ID,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        }
        params = get_oauth_params(conn)
        assert params["provider_name"] == "microsoft"
        assert TENANT_ID in params["authorize_url"]
        assert TENANT_ID in params["token_url"]
        assert "login.microsoftonline.com" in params["authorize_url"]
        assert "powerbi" in params["scopes"]
        logger.info(f"PowerBI authorize_url: {params['authorize_url']}")

    def test_fabric_params(self):
        """get_oauth_params returns correct Entra URLs for Fabric."""
        from unittest.mock import MagicMock
        from app.services.connection_oauth_service import get_oauth_params

        conn = MagicMock()
        conn.id = "test-fabric"
        conn.type = "ms_fabric"
        conn.decrypt_credentials.return_value = {
            "tenant_id": TENANT_ID,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        }
        params = get_oauth_params(conn)
        assert params["provider_name"] == "microsoft"
        assert "api.fabric.microsoft.com" in params["scopes"]
        logger.info(f"Fabric token_url: {params['token_url']}")


class TestAuthorizeEndpoint:
    """Test /connections/{id}/oauth/authorize with real Entra credentials."""

    def test_authorize_url_points_to_real_entra(
        self, test_client, login_user, create_connection, create_user, whoami
    ):
        """Authorize URL points to the real Entra tenant with correct params."""
        user = create_user()
        token = login_user(user["email"], user["password"])
        me = whoami(token)
        org_id = me["organizations"][0]["id"]

        conn = create_connection(
            name="Real Fabric OAuth",
            type="ms_fabric",
            config={"server_hostname": FABRIC_SERVER or "test.fabric.microsoft.com", "database": FABRIC_DATABASE},
            credentials={
                "tenant_id": TENANT_ID,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
            auth_policy="user_required",
            allowed_user_auth_modes=["oauth"],
            user_token=token,
            org_id=org_id,
        )

        response = test_client.get(
            f"/api/connections/{conn['id']}/oauth/authorize",
            headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
        )

        assert response.status_code == 200
        url = response.json()["authorization_url"]
        logger.info(f"Authorize URL: {url}")

        assert f"login.microsoftonline.com/{TENANT_ID}" in url
        assert f"client_id={CLIENT_ID}" in url
        assert "code_challenge=" in url
        assert "response_type=code" in url
        assert "api.fabric.microsoft.com" in url

    def test_authorize_powerbi_url(
        self, test_client, login_user, create_connection, create_user, whoami
    ):
        """PowerBI authorize URL has correct PowerBI scope."""
        user = create_user()
        token = login_user(user["email"], user["password"])
        me = whoami(token)
        org_id = me["organizations"][0]["id"]

        conn = create_connection(
            name="Real PowerBI OAuth",
            type="powerbi",
            config={},
            credentials={
                "tenant_id": TENANT_ID,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
            auth_policy="user_required",
            allowed_user_auth_modes=["oauth"],
            user_token=token,
            org_id=org_id,
        )

        response = test_client.get(
            f"/api/connections/{conn['id']}/oauth/authorize",
            headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
        )

        assert response.status_code == 200
        url = response.json()["authorization_url"]
        assert "powerbi" in url.lower() or "analysis.windows.net" in url
        logger.info(f"PowerBI authorize URL: {url}")


class TestIsEntraProvider:
    """Test _is_entra_provider with the real OIDC config."""

    def test_detects_entra_from_config(self):
        """The 'entra' OIDC provider in bow-config is detected as Entra."""
        from app.services.auth_providers import _is_entra_provider
        # This test depends on the bow-config.dev.yaml having the entra provider
        result = _is_entra_provider("entra")
        logger.info(f"_is_entra_provider('entra') = {result}")
        # Should be True if the config is loaded
        assert result is True

    def test_rejects_okta(self):
        """The 'okta' OIDC provider is not detected as Entra."""
        from app.services.auth_providers import _is_entra_provider
        result = _is_entra_provider("okta")
        # Okta is not Entra even if configured
        assert result is False


# ===========================================================================
# ROPC-based tests (non-interactive, using real user credentials)
# ===========================================================================

class TestROPCLogin:
    """Test ROPC login works for both demo users."""

    @pytest.mark.asyncio
    async def test_demo1_login(self):
        _skip_if_no_secret()
        token_data = await _ropc_login(DEMO1_EMAIL, DEMO1_PASSWORD)
        assert "access_token" in token_data
        logger.info(f"demo1 ROPC login OK — expires_in: {token_data.get('expires_in')}s")

    @pytest.mark.asyncio
    async def test_demo2_login(self):
        _skip_if_no_secret()
        token_data = await _ropc_login(DEMO2_EMAIL, DEMO2_PASSWORD)
        assert "access_token" in token_data
        logger.info(f"demo2 ROPC login OK — expires_in: {token_data.get('expires_in')}s")


class TestOBOExchange:
    """Test OBO token exchange from ROPC login token to resource-scoped token."""

    @pytest.mark.asyncio
    async def test_obo_fabric_demo1(self):
        """OBO exchange: demo1 login -> Fabric-scoped token."""
        _skip_if_no_secret()
        login_data = await _ropc_login(DEMO1_EMAIL, DEMO1_PASSWORD)
        obo_data = await _obo_exchange(login_data["access_token"], FABRIC_SCOPE)
        assert "access_token" in obo_data
        logger.info(f"OBO Fabric token for demo1 OK — expires_in: {obo_data.get('expires_in')}s")

    @pytest.mark.asyncio
    async def test_obo_fabric_demo2(self):
        """OBO exchange: demo2 login -> Fabric-scoped token."""
        _skip_if_no_secret()
        login_data = await _ropc_login(DEMO2_EMAIL, DEMO2_PASSWORD)
        obo_data = await _obo_exchange(login_data["access_token"], FABRIC_SCOPE)
        assert "access_token" in obo_data
        logger.info(f"OBO Fabric token for demo2 OK — expires_in: {obo_data.get('expires_in')}s")

    @pytest.mark.asyncio
    async def test_obo_powerbi_demo1(self):
        """OBO exchange: demo1 login -> PowerBI-scoped token."""
        _skip_if_no_secret()
        login_data = await _ropc_login(DEMO1_EMAIL, DEMO1_PASSWORD)
        obo_data = await _obo_exchange(login_data["access_token"], POWERBI_SCOPE)
        assert "access_token" in obo_data
        logger.info(f"OBO PowerBI token for demo1 OK — expires_in: {obo_data.get('expires_in')}s")


class TestOBOServiceFunction:
    """Test exchange_obo_token() service function with real Entra tokens."""

    @pytest.mark.asyncio
    async def test_exchange_obo_token_fabric(self):
        """exchange_obo_token() works for Fabric with real Entra token."""
        _skip_if_no_secret()
        from unittest.mock import MagicMock
        from app.services.connection_oauth_service import exchange_obo_token

        login_data = await _ropc_login(DEMO1_EMAIL, DEMO1_PASSWORD)

        conn = MagicMock()
        conn.id = "test-fabric-obo"
        conn.type = "ms_fabric"
        conn.decrypt_credentials.return_value = {
            "tenant_id": TENANT_ID,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        }

        tokens = await exchange_obo_token(login_data["access_token"], conn)
        assert tokens["access_token"]
        assert tokens["token_type"] == "Bearer"
        assert tokens["expires_at"]
        logger.info(f"exchange_obo_token() Fabric OK — expires_at: {tokens['expires_at']}")

    @pytest.mark.asyncio
    async def test_exchange_obo_token_powerbi(self):
        """exchange_obo_token() works for PowerBI with real Entra token."""
        _skip_if_no_secret()
        from unittest.mock import MagicMock
        from app.services.connection_oauth_service import exchange_obo_token

        login_data = await _ropc_login(DEMO1_EMAIL, DEMO1_PASSWORD)

        conn = MagicMock()
        conn.id = "test-pbi-obo"
        conn.type = "powerbi"
        conn.decrypt_credentials.return_value = {
            "tenant_id": TENANT_ID,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        }

        tokens = await exchange_obo_token(login_data["access_token"], conn)
        assert tokens["access_token"]
        assert tokens["token_type"] == "Bearer"
        logger.info(f"exchange_obo_token() PowerBI OK — expires_at: {tokens['expires_at']}")


class TestFabricClientDelegated:
    """Test Fabric client with delegated (user-level) tokens.

    Requires BOW_FABRIC_SERVER env var and pyodbc + ODBC Driver 18.
    """

    @pytest.mark.asyncio
    async def test_demo1_sees_all_tables(self):
        """demo1 (AllFabric group) can see both sales and finance tables."""
        _skip_if_no_secret()
        if not FABRIC_SERVER:
            pytest.skip("BOW_FABRIC_SERVER not set")

        login_data = await _ropc_login(DEMO1_EMAIL, DEMO1_PASSWORD)
        obo_data = await _obo_exchange(login_data["access_token"], FABRIC_SCOPE)

        from app.data_sources.clients.ms_fabric_client import MSFabricClient
        client = MSFabricClient(
            server_hostname=FABRIC_SERVER,
            database=FABRIC_DATABASE,
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            access_token=obo_data["access_token"],
        )
        client.connect()
        schemas = client.get_schemas()

        table_names = set()
        for schema in schemas:
            for table in schema.get("tables", []):
                table_names.add(table["name"].lower())

        logger.info(f"demo1 tables: {table_names}")
        assert "sales" in table_names, f"demo1 should see 'sales', got: {table_names}"
        assert "finance" in table_names, f"demo1 should see 'finance', got: {table_names}"

    @pytest.mark.asyncio
    async def test_demo2_sees_only_sales(self):
        """demo2 (MinimalFabric group) can see sales but NOT finance."""
        _skip_if_no_secret()
        if not FABRIC_SERVER:
            pytest.skip("BOW_FABRIC_SERVER not set")

        login_data = await _ropc_login(DEMO2_EMAIL, DEMO2_PASSWORD)
        obo_data = await _obo_exchange(login_data["access_token"], FABRIC_SCOPE)

        from app.data_sources.clients.ms_fabric_client import MSFabricClient
        client = MSFabricClient(
            server_hostname=FABRIC_SERVER,
            database=FABRIC_DATABASE,
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            access_token=obo_data["access_token"],
        )
        client.connect()
        schemas = client.get_schemas()

        table_names = set()
        for schema in schemas:
            for table in schema.get("tables", []):
                table_names.add(table["name"].lower())

        logger.info(f"demo2 tables: {table_names}")
        assert "sales" in table_names, f"demo2 should see 'sales', got: {table_names}"
        assert "finance" not in table_names, f"demo2 should NOT see 'finance', got: {table_names}"

    @pytest.mark.asyncio
    async def test_demo1_query_finance(self):
        """demo1 can query the finance table and get correct data."""
        _skip_if_no_secret()
        if not FABRIC_SERVER:
            pytest.skip("BOW_FABRIC_SERVER not set")

        login_data = await _ropc_login(DEMO1_EMAIL, DEMO1_PASSWORD)
        obo_data = await _obo_exchange(login_data["access_token"], FABRIC_SCOPE)

        from app.data_sources.clients.ms_fabric_client import MSFabricClient
        client = MSFabricClient(
            server_hostname=FABRIC_SERVER,
            database=FABRIC_DATABASE,
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            access_token=obo_data["access_token"],
        )
        client.connect()

        result = client.execute_query("SELECT * FROM dbo.finance ORDER BY id")
        logger.info(f"demo1 finance query: {result}")
        assert len(result) >= 2
        depts = [row["department"] for row in result]
        assert "Engineering" in depts
        assert "Marketing" in depts

    @pytest.mark.asyncio
    async def test_demo1_query_sales(self):
        """demo1 can query the sales table and get correct data."""
        _skip_if_no_secret()
        if not FABRIC_SERVER:
            pytest.skip("BOW_FABRIC_SERVER not set")

        login_data = await _ropc_login(DEMO1_EMAIL, DEMO1_PASSWORD)
        obo_data = await _obo_exchange(login_data["access_token"], FABRIC_SCOPE)

        from app.data_sources.clients.ms_fabric_client import MSFabricClient
        client = MSFabricClient(
            server_hostname=FABRIC_SERVER,
            database=FABRIC_DATABASE,
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            access_token=obo_data["access_token"],
        )
        client.connect()

        result = client.execute_query("SELECT * FROM dbo.sales ORDER BY id")
        logger.info(f"demo1 sales query: {result}")
        assert len(result) >= 2
        regions = [row["region"] for row in result]
        assert "EMEA" in regions
        assert "US" in regions


class TestTokenRefreshFlow:
    """Test token refresh with real Entra tokens.

    Tests the realistic flow: login → OBO → get refresh_token → refresh it.
    """

    @pytest.mark.asyncio
    async def test_refresh_obo_powerbi_token(self):
        """Refresh token from OBO PowerBI exchange can get a new access token."""
        _skip_if_no_secret()
        from app.services.connection_oauth_service import refresh_access_token

        # Step 1: Login
        login_data = await _ropc_login(DEMO1_EMAIL, DEMO1_PASSWORD, scope=LOGIN_SCOPE)

        # Step 2: OBO to get PowerBI token (includes refresh_token)
        obo_data = await _obo_exchange(login_data["access_token"], POWERBI_SCOPE)
        refresh_token = obo_data.get("refresh_token")
        if not refresh_token:
            pytest.skip("OBO did not return a refresh_token")

        # Step 3: Use our service function to refresh the OBO token
        new_tokens = await refresh_access_token(
            oauth_params={
                "token_url": f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
            refresh_token=refresh_token,
        )

        assert new_tokens["access_token"]
        assert new_tokens["access_token"] != obo_data["access_token"]
        logger.info(f"OBO token refresh OK — new expires_at: {new_tokens['expires_at']}")


# ===========================================================================
# Interactive tests (require browser authentication via device code)
# Fallback if ROPC is disabled.
# ===========================================================================

class TestInteractiveOBOExchange:
    """Test OBO token exchange with real user tokens. Requires browser auth."""

    @pytest.mark.asyncio
    async def test_obo_fabric_demo1(self):
        """OBO exchange: demo1 login token -> Fabric-scoped token."""
        _skip_if_no_secret()
        login_data = await _device_code_login(
            scope=f"{LOGIN_SCOPE} offline_access",
            label=f"demo1 ({DEMO1_EMAIL})",
        )
        obo_data = await _obo_exchange(login_data["access_token"], FABRIC_SCOPE)
        assert "access_token" in obo_data
        logger.info(f"OBO Fabric token for demo1 OK — expires_in: {obo_data.get('expires_in')}s")

    @pytest.mark.asyncio
    async def test_obo_powerbi_demo1(self):
        """OBO exchange: demo1 login token -> PowerBI-scoped token."""
        _skip_if_no_secret()
        login_data = await _device_code_login(
            scope=f"{LOGIN_SCOPE} offline_access",
            label=f"demo1 ({DEMO1_EMAIL})",
        )
        obo_data = await _obo_exchange(login_data["access_token"], POWERBI_SCOPE)
        assert "access_token" in obo_data
        logger.info(f"OBO PowerBI token for demo1 OK — expires_in: {obo_data.get('expires_in')}s")


class TestInteractiveOBOServiceFunction:
    """Test exchange_obo_token() service function with real tokens."""

    @pytest.mark.asyncio
    async def test_exchange_obo_token_fabric(self):
        """exchange_obo_token() works for Fabric with real Entra token."""
        _skip_if_no_secret()
        from unittest.mock import MagicMock
        from app.services.connection_oauth_service import exchange_obo_token

        login_data = await _device_code_login(
            scope=f"{LOGIN_SCOPE} offline_access",
            label=f"demo1 ({DEMO1_EMAIL})",
        )

        conn = MagicMock()
        conn.id = "test-fabric-obo"
        conn.type = "ms_fabric"
        conn.decrypt_credentials.return_value = {
            "tenant_id": TENANT_ID,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        }

        tokens = await exchange_obo_token(login_data["access_token"], conn)
        assert tokens["access_token"]
        assert tokens["token_type"] == "Bearer"
        assert tokens["expires_at"]
        logger.info(f"exchange_obo_token() OK — expires_at: {tokens['expires_at']}")


class TestInteractiveFabricClient:
    """Test Fabric client with delegated tokens.

    Requires FABRIC_SERVER env var and pyodbc + ODBC Driver 18.
    """

    @pytest.mark.asyncio
    async def test_demo1_sees_all_tables(self):
        """demo1 (AllFabric group) can see both sales and finance tables."""
        _skip_if_no_secret()
        if not FABRIC_SERVER:
            pytest.skip("BOW_FABRIC_SERVER not set")

        login_data = await _device_code_login(
            scope=f"{LOGIN_SCOPE} offline_access",
            label=f"demo1 ({DEMO1_EMAIL})",
        )
        obo_data = await _obo_exchange(login_data["access_token"], FABRIC_SCOPE)
        access_token = obo_data["access_token"]

        from app.data_sources.clients.ms_fabric_client import MSFabricClient
        client = MSFabricClient(
            server_hostname=FABRIC_SERVER,
            database=FABRIC_DATABASE,
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            access_token=access_token,
        )
        client.connect()
        schemas = client.get_schemas()

        table_names = set()
        for schema in schemas:
            for table in schema.get("tables", []):
                table_names.add(table["name"].lower())

        logger.info(f"demo1 tables: {table_names}")
        assert "sales" in table_names, f"demo1 should see 'sales', got: {table_names}"
        assert "finance" in table_names, f"demo1 should see 'finance', got: {table_names}"

    @pytest.mark.asyncio
    async def test_demo2_sees_only_sales(self):
        """demo2 (MinimalFabric group) can see sales but NOT finance."""
        _skip_if_no_secret()
        if not FABRIC_SERVER:
            pytest.skip("BOW_FABRIC_SERVER not set")

        login_data = await _device_code_login(
            scope=f"{LOGIN_SCOPE} offline_access",
            label=f"demo2 ({DEMO2_EMAIL})",
        )
        obo_data = await _obo_exchange(login_data["access_token"], FABRIC_SCOPE)
        access_token = obo_data["access_token"]

        from app.data_sources.clients.ms_fabric_client import MSFabricClient
        client = MSFabricClient(
            server_hostname=FABRIC_SERVER,
            database=FABRIC_DATABASE,
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            access_token=access_token,
        )
        client.connect()
        schemas = client.get_schemas()

        table_names = set()
        for schema in schemas:
            for table in schema.get("tables", []):
                table_names.add(table["name"].lower())

        logger.info(f"demo2 tables: {table_names}")
        assert "sales" in table_names, f"demo2 should see 'sales', got: {table_names}"
        assert "finance" not in table_names, f"demo2 should NOT see 'finance', got: {table_names}"

    @pytest.mark.asyncio
    async def test_demo1_query_finance(self):
        """demo1 can query the finance table and get correct data."""
        _skip_if_no_secret()
        if not FABRIC_SERVER:
            pytest.skip("BOW_FABRIC_SERVER not set")

        login_data = await _device_code_login(
            scope=f"{LOGIN_SCOPE} offline_access",
            label=f"demo1 ({DEMO1_EMAIL})",
        )
        obo_data = await _obo_exchange(login_data["access_token"], FABRIC_SCOPE)

        from app.data_sources.clients.ms_fabric_client import MSFabricClient
        client = MSFabricClient(
            server_hostname=FABRIC_SERVER,
            database=FABRIC_DATABASE,
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            access_token=obo_data["access_token"],
        )
        client.connect()

        result = client.execute_query("SELECT * FROM dbo.finance ORDER BY id")
        logger.info(f"demo1 finance query result: {result}")
        assert len(result) >= 2
        depts = [row["department"] for row in result]
        assert "Engineering" in depts
        assert "Marketing" in depts

    @pytest.mark.asyncio
    async def test_demo1_query_sales(self):
        """demo1 can query the sales table and get correct data."""
        _skip_if_no_secret()
        if not FABRIC_SERVER:
            pytest.skip("BOW_FABRIC_SERVER not set")

        login_data = await _device_code_login(
            scope=f"{LOGIN_SCOPE} offline_access",
            label=f"demo1 ({DEMO1_EMAIL})",
        )
        obo_data = await _obo_exchange(login_data["access_token"], FABRIC_SCOPE)

        from app.data_sources.clients.ms_fabric_client import MSFabricClient
        client = MSFabricClient(
            server_hostname=FABRIC_SERVER,
            database=FABRIC_DATABASE,
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            access_token=obo_data["access_token"],
        )
        client.connect()

        result = client.execute_query("SELECT * FROM dbo.sales ORDER BY id")
        logger.info(f"demo1 sales query result: {result}")
        assert len(result) >= 2
        regions = [row["region"] for row in result]
        assert "EMEA" in regions
        assert "US" in regions


class TestInteractiveTokenRefresh:
    """Test token refresh with real Entra tokens."""

    @pytest.mark.asyncio
    async def test_refresh_token_works(self):
        """Refresh token from device code can be used to get new access token."""
        _skip_if_no_secret()
        from app.services.connection_oauth_service import refresh_access_token

        login_data = await _device_code_login(
            scope=f"{LOGIN_SCOPE} offline_access",
            label=f"demo1 ({DEMO1_EMAIL})",
        )

        refresh_token = login_data.get("refresh_token")
        if not refresh_token:
            pytest.skip("No refresh_token returned")

        new_tokens = await refresh_access_token(
            oauth_params={
                "token_url": f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
            refresh_token=refresh_token,
        )

        assert new_tokens["access_token"]
        assert new_tokens["access_token"] != login_data["access_token"]
        logger.info(f"Token refresh OK — new expires_at: {new_tokens['expires_at']}")
