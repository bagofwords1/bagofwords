"""
Unit tests for connection OAuth service.

Tests get_oauth_params(), token exchange, and token refresh logic
using httpx.MockTransport (no real OAuth providers needed).
"""
import json
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, AsyncMock, patch

import pytest
import httpx

from app.services.connection_oauth_service import (
    generate_pkce_pair,
    get_oauth_params,
    exchange_code_for_tokens,
    refresh_access_token,
    exchange_obo_token,
    parse_expires_at,
    ENTRA_OBO_CONNECTION_TYPES,
)


def _make_connection(type="powerbi", credentials=None, id="test-conn"):
    """Create a mock Connection object for testing."""
    conn = MagicMock()
    conn.id = id
    conn.type = type
    conn.organization_id = "org-1"
    conn.decrypt_credentials.return_value = credentials or {}
    return conn


# ---------------------------------------------------------------------------
# PKCE Tests
# ---------------------------------------------------------------------------

class TestPKCE:
    def test_generate_pkce_pair(self):
        verifier, challenge = generate_pkce_pair()
        assert len(verifier) >= 43
        assert len(challenge) > 0
        assert verifier != challenge

    def test_pkce_pair_unique(self):
        pair1 = generate_pkce_pair()
        pair2 = generate_pkce_pair()
        assert pair1[0] != pair2[0]
        assert pair1[1] != pair2[1]


# ---------------------------------------------------------------------------
# get_oauth_params Tests
# ---------------------------------------------------------------------------

class TestGetOAuthParams:
    def test_powerbi(self):
        conn = _make_connection(
            type="powerbi",
            credentials={"tenant_id": "t1", "client_id": "c1", "client_secret": "s1"}
        )
        params = get_oauth_params(conn)
        assert params["provider_name"] == "microsoft"
        assert "t1" in params["authorize_url"]
        assert "t1" in params["token_url"]
        assert params["client_id"] == "c1"
        assert params["client_secret"] == "s1"
        assert "powerbi" in params["scopes"]

    def test_powerbi_oauth_override(self):
        conn = _make_connection(
            type="powerbi",
            credentials={
                "tenant_id": "t1",
                "client_id": "c1", "client_secret": "s1",
                "oauth_client_id": "oc1", "oauth_client_secret": "os1",
            }
        )
        params = get_oauth_params(conn)
        assert params["client_id"] == "oc1"
        assert params["client_secret"] == "os1"

    def test_ms_fabric(self):
        conn = _make_connection(
            type="ms_fabric",
            credentials={"tenant_id": "t2", "client_id": "c2", "client_secret": "s2"}
        )
        params = get_oauth_params(conn)
        assert params["provider_name"] == "microsoft"
        # Fabric Warehouse/Lakehouse SQL endpoints want an Azure SQL token
        # (aud=database.windows.net), not a Fabric API token — the latter is
        # rejected by the SQL endpoint with login error 18456.
        assert "database.windows.net/user_impersonation" in params["scopes"]

    def test_bigquery(self):
        conn = _make_connection(
            type="bigquery",
            credentials={"oauth_client_id": "gc1", "oauth_client_secret": "gs1"}
        )
        params = get_oauth_params(conn)
        assert params["provider_name"] == "google"
        assert params["client_id"] == "gc1"
        assert "bigquery" in params["scopes"]
        assert "accounts.google.com" in params["authorize_url"]

    def test_bigquery_no_oauth_creds_raises(self):
        conn = _make_connection(
            type="bigquery",
            credentials={"credentials_json": "{}"}
        )
        with pytest.raises(ValueError, match="oauth_client_id"):
            get_oauth_params(conn)

    # -- Snowflake ----------------------------------------------------------
    # Snowflake's built-in OAuth server has account-specific endpoints derived
    # from the account identifier in config — the ServiceNow pattern, not the
    # Microsoft constant-endpoint one.

    def test_snowflake(self):
        conn = _make_connection(
            type="snowflake",
            credentials={
                "user": "SVC_USER", "password": "p",
                "oauth_client_id": "sf-client", "oauth_client_secret": "sf-secret",
            },
        )
        conn.config = {"account": "myorg-myaccount", "warehouse": "WH", "database": "DB", "schema": "PUBLIC"}
        params = get_oauth_params(conn)
        assert params["provider_name"] == "snowflake"
        assert params["authorize_url"] == "https://myorg-myaccount.snowflakecomputing.com/oauth/authorize"
        assert params["token_url"] == "https://myorg-myaccount.snowflakecomputing.com/oauth/token-request"
        assert params["client_id"] == "sf-client"
        assert params["client_secret"] == "sf-secret"
        # Refresh tokens must be requested explicitly via the refresh_token scope.
        assert params["scopes"] == "refresh_token"
        # Snowflake's token endpoint authenticates confidential clients with Basic auth.
        assert params["token_endpoint_auth_method"] == "client_secret_basic"

    def test_snowflake_underscores_become_hyphens_in_host(self):
        """Account identifiers may carry underscores; account URLs use hyphens."""
        conn = _make_connection(
            type="snowflake",
            credentials={"oauth_client_id": "c", "oauth_client_secret": "s"},
        )
        conn.config = {"account": "MY_ORG-MY_ACCOUNT"}
        params = get_oauth_params(conn)
        assert params["authorize_url"].startswith("https://my-org-my-account.snowflakecomputing.com/")

    def test_snowflake_role_adds_session_role_scope(self):
        """A pinned role must be in the token's scope or the client's role= connect arg fails."""
        conn = _make_connection(
            type="snowflake",
            credentials={"oauth_client_id": "c", "oauth_client_secret": "s"},
        )
        conn.config = {"account": "acct", "role": "ANALYST"}
        params = get_oauth_params(conn)
        assert params["scopes"] == "refresh_token session:role:ANALYST"

    def test_snowflake_config_stored_as_json_string(self):
        conn = _make_connection(
            type="snowflake",
            credentials={"oauth_client_id": "c", "oauth_client_secret": "s"},
        )
        conn.config = json.dumps({"account": "acct"})
        params = get_oauth_params(conn)
        assert params["token_url"] == "https://acct.snowflakecomputing.com/oauth/token-request"

    def test_snowflake_missing_oauth_creds_raises(self):
        conn = _make_connection(
            type="snowflake",
            credentials={"user": "u", "password": "p"},
        )
        conn.config = {"account": "acct"}
        with pytest.raises(ValueError, match="oauth_client_id"):
            get_oauth_params(conn)

    def test_snowflake_missing_account_raises(self):
        conn = _make_connection(
            type="snowflake",
            credentials={"oauth_client_id": "c", "oauth_client_secret": "s"},
        )
        conn.config = {}
        with pytest.raises(ValueError, match="account"):
            get_oauth_params(conn)

    def test_monday(self):
        conn = _make_connection(
            type="monday",
            credentials={
                "api_token": "svc-token",
                "oauth_client_id": "mc1", "oauth_client_secret": "ms1",
            },
        )
        params = get_oauth_params(conn)
        assert params["provider_name"] == "monday"
        assert params["authorize_url"] == "https://auth.monday.com/oauth2/authorize"
        assert params["token_url"] == "https://auth.monday.com/oauth2/token"
        assert params["client_id"] == "mc1"
        assert params["client_secret"] == "ms1"
        assert params["scopes"] == "me:read boards:read workspaces:read users:read account:read"

    def test_monday_missing_oauth_creds_raises(self):
        conn = _make_connection(type="monday", credentials={"api_token": "svc-token"})
        with pytest.raises(ValueError, match="oauth_client_id"):
            get_oauth_params(conn)

    def test_servicenow(self):
        conn = _make_connection(
            type="servicenow",
            credentials={
                "username": "u", "password": "p",
                "oauth_client_id": "sc1", "oauth_client_secret": "ss1",
            },
        )
        conn.config = {"instance_url": "https://acme.service-now.com/"}
        params = get_oauth_params(conn)
        assert params["provider_name"] == "servicenow"
        assert params["authorize_url"] == "https://acme.service-now.com/oauth_auth.do"
        assert params["token_url"] == "https://acme.service-now.com/oauth_token.do"
        assert params["client_id"] == "sc1"
        assert params["client_secret"] == "ss1"
        assert params["scopes"] == "useraccount"

    def test_servicenow_config_stored_as_json_string(self):
        conn = _make_connection(type="servicenow", credentials={"oauth_client_id": "sc1"})
        conn.config = json.dumps({"instance_url": "https://acme.service-now.com"})
        params = get_oauth_params(conn)
        assert params["authorize_url"] == "https://acme.service-now.com/oauth_auth.do"

    def test_servicenow_public_client_needs_no_secret(self):
        conn = _make_connection(type="servicenow", credentials={"oauth_client_id": "sc1"})
        conn.config = {"instance_url": "https://acme.service-now.com"}
        params = get_oauth_params(conn)
        assert params["client_secret"] is None

    def test_servicenow_missing_client_id_raises(self):
        conn = _make_connection(type="servicenow", credentials={"username": "u", "password": "p"})
        conn.config = {"instance_url": "https://acme.service-now.com"}
        with pytest.raises(ValueError, match="oauth_client_id"):
            get_oauth_params(conn)

    def test_servicenow_missing_instance_url_raises(self):
        conn = _make_connection(type="servicenow", credentials={"oauth_client_id": "sc1"})
        conn.config = {}
        with pytest.raises(ValueError, match="instance_url"):
            get_oauth_params(conn)

    # -- Priority ERP -------------------------------------------------------
    # Priority's OAuth2 is on-premise only (its own guide is scoped to
    # "on-prem (non-SaaS) installations") and needs the paid External ID
    # module. Endpoints are per-tenant, derived from the OData service root in
    # config — the ServiceNow pattern, not the Microsoft constant-endpoint one.

    def test_priority_erp(self):
        conn = _make_connection(
            type="priority_erp",
            credentials={"pat": "t", "oauth_client_id": "app-id", "oauth_client_secret": "secret-id"},
        )
        conn.config = {"service_root": "https://priority.acme.local/odata/Priority/tabula.ini/acme"}
        params = get_oauth_params(conn)
        assert params["provider_name"] == "priority_erp"
        assert params["authorize_url"] == "https://priority.acme.local/accounts/connect/authorize"
        assert params["token_url"] == "https://priority.acme.local/accounts/connect/token"
        # Priority documents exactly this scope pair for the REST API.
        assert params["scopes"] == "openid rest_api"
        # "Client Authentication: Send as Basic Auth header" — Priority rejects
        # a body-carried secret.
        assert params["token_endpoint_auth_method"] == "client_secret_basic"

    def test_priority_erp_strips_everything_from_the_odata_segment(self):
        """PRIORITY_DOMAIN is "whatever comes before the 'odata' segment"."""
        conn = _make_connection(
            type="priority_erp",
            credentials={"oauth_client_id": "a", "oauth_client_secret": "b"},
        )
        conn.config = {"service_root": "https://p.example.com/odata/Priority/tabula.ini/co/"}
        assert get_oauth_params(conn)["authorize_url"] == \
            "https://p.example.com/accounts/connect/authorize"

    def test_priority_erp_preserves_iis_virtual_directory(self):
        """Some on-prem IIS deployments host Priority under a sub-path, which
        must survive the domain derivation."""
        conn = _make_connection(
            type="priority_erp",
            credentials={"oauth_client_id": "a", "oauth_client_secret": "b"},
        )
        conn.config = {"service_root": "https://host.example.com/prioritysrv/odata/Priority/tabula.ini/co"}
        assert get_oauth_params(conn)["token_url"] == \
            "https://host.example.com/prioritysrv/accounts/connect/token"

    def test_priority_erp_config_stored_as_json_string(self):
        conn = _make_connection(
            type="priority_erp",
            credentials={"oauth_client_id": "a", "oauth_client_secret": "b"},
        )
        conn.config = json.dumps({"service_root": "https://p.example.com/odata/Priority/t.ini/c"})
        assert get_oauth_params(conn)["authorize_url"] == \
            "https://p.example.com/accounts/connect/authorize"

    def test_priority_erp_requires_a_client_secret(self):
        """Priority always issues a Secret ID and authenticates the client with
        Basic, so unlike ServiceNow there is no public-client mode."""
        conn = _make_connection(type="priority_erp", credentials={"oauth_client_id": "app-id"})
        conn.config = {"service_root": "https://p.example.com/odata/Priority/t.ini/c"}
        with pytest.raises(ValueError, match="oauth_client_secret"):
            get_oauth_params(conn)

    def test_priority_erp_missing_service_root_raises(self):
        conn = _make_connection(
            type="priority_erp",
            credentials={"oauth_client_id": "a", "oauth_client_secret": "b"},
        )
        conn.config = {}
        with pytest.raises(ValueError, match="service_root"):
            get_oauth_params(conn)

    def test_priority_erp_non_url_service_root_raises(self):
        conn = _make_connection(
            type="priority_erp",
            credentials={"oauth_client_id": "a", "oauth_client_secret": "b"},
        )
        conn.config = {"service_root": "not-a-url"}
        with pytest.raises(ValueError, match="Priority OData URL"):
            get_oauth_params(conn)

    def test_unsupported_type_raises(self):
        conn = _make_connection(type="postgres", credentials={})
        with pytest.raises(ValueError, match="not supported"):
            get_oauth_params(conn)

    def test_powerbi_missing_tenant_raises(self):
        conn = _make_connection(
            type="powerbi",
            credentials={"client_id": "c1", "client_secret": "s1"}
        )
        with pytest.raises(ValueError, match="tenant_id"):
            get_oauth_params(conn)


# ---------------------------------------------------------------------------
# Token Exchange Tests (mock HTTP)
# ---------------------------------------------------------------------------

class TestTokenExchange:
    @pytest.mark.asyncio
    async def test_exchange_code_success(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = dict(urllib.parse.parse_qsl(request.content.decode()))
            return httpx.Response(200, json={
                "access_token": "at_123",
                "refresh_token": "rt_456",
                "expires_in": 3600,
                "token_type": "Bearer",
            })

        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient
        class _Patched(original):
            def __init__(self, *a, **kw):
                kw["transport"] = transport
                super().__init__(*a, **kw)
        monkeypatch.setattr(httpx, "AsyncClient", _Patched)

        oauth_params = {
            "token_url": "https://login.example.com/token",
            "client_id": "c1",
            "client_secret": "s1",
        }
        tokens = await exchange_code_for_tokens(
            oauth_params, code="auth_code_123", redirect_uri="https://app/callback",
            code_verifier="verifier_abc"
        )

        assert tokens["access_token"] == "at_123"
        assert tokens["refresh_token"] == "rt_456"
        assert "expires_at" in tokens

        # Verify request params
        assert captured["body"]["grant_type"] == "authorization_code"
        assert captured["body"]["code"] == "auth_code_123"
        assert captured["body"]["code_verifier"] == "verifier_abc"
        assert captured["body"]["client_id"] == "c1"

    @pytest.mark.asyncio
    async def test_exchange_code_error(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "invalid_grant"})

        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient
        class _Patched(original):
            def __init__(self, *a, **kw):
                kw["transport"] = transport
                super().__init__(*a, **kw)
        monkeypatch.setattr(httpx, "AsyncClient", _Patched)

        oauth_params = {
            "token_url": "https://login.example.com/token",
            "client_id": "c1",
            "client_secret": "s1",
        }
        with pytest.raises(ValueError, match="token exchange failed"):
            await exchange_code_for_tokens(oauth_params, code="bad_code", redirect_uri="https://app/callback")


class TestTokenRefresh:
    @pytest.mark.asyncio
    async def test_refresh_success(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = dict(urllib.parse.parse_qsl(request.content.decode()))
            return httpx.Response(200, json={
                "access_token": "new_at",
                "refresh_token": "new_rt",
                "expires_in": 7200,
                "token_type": "Bearer",
            })

        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient
        class _Patched(original):
            def __init__(self, *a, **kw):
                kw["transport"] = transport
                super().__init__(*a, **kw)
        monkeypatch.setattr(httpx, "AsyncClient", _Patched)

        oauth_params = {
            "token_url": "https://login.example.com/token",
            "client_id": "c1",
            "client_secret": "s1",
        }
        tokens = await refresh_access_token(oauth_params, refresh_token="old_rt")

        assert tokens["access_token"] == "new_at"
        assert tokens["refresh_token"] == "new_rt"
        assert captured["body"]["grant_type"] == "refresh_token"
        assert captured["body"]["refresh_token"] == "old_rt"

    @pytest.mark.asyncio
    async def test_refresh_error(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "invalid_grant"})

        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient
        class _Patched(original):
            def __init__(self, *a, **kw):
                kw["transport"] = transport
                super().__init__(*a, **kw)
        monkeypatch.setattr(httpx, "AsyncClient", _Patched)

        oauth_params = {
            "token_url": "https://login.example.com/token",
            "client_id": "c1",
            "client_secret": "s1",
        }
        with pytest.raises(ValueError, match="refresh failed"):
            await refresh_access_token(oauth_params, refresh_token="expired_rt")


# ---------------------------------------------------------------------------
# Client authentication at the token endpoint (client_secret_basic vs _post)
# ---------------------------------------------------------------------------

def _capturing_transport(monkeypatch, captured):
    """Patch httpx.AsyncClient to a MockTransport that records the request."""
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth_header"] = request.headers.get("authorization")
        captured["body"] = dict(urllib.parse.parse_qsl(request.content.decode()))
        return httpx.Response(200, json={
            "access_token": "at", "refresh_token": "rt",
            "expires_in": 3600, "token_type": "Bearer",
        })

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class _Patched(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _Patched)


class TestClientAuthMethods:
    """X requires HTTP Basic auth; Microsoft/Google use client_secret in body."""

    @pytest.mark.asyncio
    async def test_exchange_basic_auth_sends_authorization_header(self, monkeypatch):
        captured = {}
        _capturing_transport(monkeypatch, captured)
        oauth_params = {
            "token_url": "https://api.x.com/2/oauth2/token",
            "client_id": "cid",
            "client_secret": "csecret",
            "token_endpoint_auth_method": "client_secret_basic",
        }
        await exchange_code_for_tokens(
            oauth_params, code="c", redirect_uri="https://app/callback",
            code_verifier="v",
        )
        # Authorization: Basic base64(client_id:client_secret)
        import base64
        expected = "Basic " + base64.b64encode(b"cid:csecret").decode()
        assert captured["auth_header"] == expected
        # The secret must NOT also be in the body, and X authenticates client_id
        # solely via the header — neither belongs in the form body.
        assert "client_secret" not in captured["body"]
        assert "client_id" not in captured["body"]
        assert captured["body"]["code_verifier"] == "v"

    @pytest.mark.asyncio
    async def test_refresh_basic_auth_sends_authorization_header(self, monkeypatch):
        captured = {}
        _capturing_transport(monkeypatch, captured)
        oauth_params = {
            "token_url": "https://api.x.com/2/oauth2/token",
            "client_id": "cid",
            "client_secret": "csecret",
            "token_endpoint_auth_method": "client_secret_basic",
        }
        await refresh_access_token(oauth_params, refresh_token="old_rt")
        assert captured["auth_header"].startswith("Basic ")
        assert "client_secret" not in captured["body"]
        assert captured["body"]["grant_type"] == "refresh_token"

    @pytest.mark.asyncio
    async def test_exchange_post_auth_keeps_secret_in_body(self, monkeypatch):
        captured = {}
        _capturing_transport(monkeypatch, captured)
        oauth_params = {
            "token_url": "https://login.microsoftonline.com/t/oauth2/v2.0/token",
            "client_id": "cid",
            "client_secret": "csecret",
            "token_endpoint_auth_method": "client_secret_post",
        }
        await exchange_code_for_tokens(oauth_params, code="c", redirect_uri="https://app/callback")
        assert captured["auth_header"] is None
        assert captured["body"]["client_secret"] == "csecret"
        assert captured["body"]["client_id"] == "cid"

    @pytest.mark.asyncio
    async def test_exchange_default_infers_post_when_secret_present(self, monkeypatch):
        # No explicit method → behaves as before (secret in body) for
        # backward-compat with connections created before the field existed.
        captured = {}
        _capturing_transport(monkeypatch, captured)
        oauth_params = {
            "token_url": "https://login.example.com/token",
            "client_id": "cid",
            "client_secret": "csecret",
        }
        await exchange_code_for_tokens(oauth_params, code="c", redirect_uri="https://app/callback")
        assert captured["auth_header"] is None
        assert captured["body"]["client_secret"] == "csecret"

    @pytest.mark.asyncio
    async def test_exchange_none_public_client_sends_no_secret(self, monkeypatch):
        captured = {}
        _capturing_transport(monkeypatch, captured)
        oauth_params = {
            "token_url": "https://mcp.example.com/token",
            "client_id": "cid",
            "client_secret": None,
            "token_endpoint_auth_method": "none",
        }
        await exchange_code_for_tokens(oauth_params, code="c", redirect_uri="https://app/callback")
        assert captured["auth_header"] is None
        assert "client_secret" not in captured["body"]

    @pytest.mark.asyncio
    async def test_basic_auth_requires_secret(self, monkeypatch):
        captured = {}
        _capturing_transport(monkeypatch, captured)
        oauth_params = {
            "token_url": "https://api.x.com/2/oauth2/token",
            "client_id": "cid",
            "client_secret": None,
            "token_endpoint_auth_method": "client_secret_basic",
        }
        with pytest.raises(ValueError, match="client_secret_basic requires a client_secret"):
            await exchange_code_for_tokens(oauth_params, code="c", redirect_uri="https://app/callback")

    def test_get_oauth_params_infers_basic_for_x_host(self):
        # An X connection created before token_endpoint_auth_method existed still
        # gets Basic auth via the api.x.com host fallback.
        conn = _make_connection(type="mcp", credentials={
            "authorize_url": "https://x.com/i/oauth2/authorize",
            "token_url": "https://api.x.com/2/oauth2/token",
            "client_id": "cid",
            "client_secret": "csecret",
            "scopes": "tweet.read tweet.write offline.access",
        })
        params = get_oauth_params(conn)
        assert params["token_endpoint_auth_method"] == "client_secret_basic"

    def test_get_oauth_params_explicit_method_wins(self):
        conn = _make_connection(type="mcp", credentials={
            "authorize_url": "https://idp.example.com/authorize",
            "token_url": "https://idp.example.com/token",
            "client_id": "cid",
            "client_secret": "csecret",
            "token_endpoint_auth_method": "client_secret_post",
        })
        params = get_oauth_params(conn)
        assert params["token_endpoint_auth_method"] == "client_secret_post"


# ---------------------------------------------------------------------------
# OBO Token Exchange Tests (Phase 2)
# ---------------------------------------------------------------------------

class TestOBOExchange:
    @pytest.mark.asyncio
    async def test_obo_exchange_powerbi(self, monkeypatch):
        """OBO exchange sends correct jwt-bearer grant for PowerBI."""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = dict(urllib.parse.parse_qsl(request.content.decode()))
            return httpx.Response(200, json={
                "access_token": "obo_at_123",
                "refresh_token": "obo_rt_456",
                "expires_in": 3600,
                "token_type": "Bearer",
            })

        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient
        class _Patched(original):
            def __init__(self, *a, **kw):
                kw["transport"] = transport
                super().__init__(*a, **kw)
        monkeypatch.setattr(httpx, "AsyncClient", _Patched)

        conn = _make_connection(
            type="powerbi",
            credentials={"tenant_id": "t1", "client_id": "c1", "client_secret": "s1"},
        )
        tokens = await exchange_obo_token("login_token_xyz", conn)

        assert tokens["access_token"] == "obo_at_123"
        assert tokens["refresh_token"] == "obo_rt_456"

        # Verify OBO grant params
        assert captured["body"]["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
        assert captured["body"]["assertion"] == "login_token_xyz"
        assert captured["body"]["requested_token_use"] == "on_behalf_of"
        assert captured["body"]["client_id"] == "c1"
        assert captured["body"]["client_secret"] == "s1"
        assert "powerbi" in captured["body"]["scope"]
        assert "t1" in captured["url"]

    @pytest.mark.asyncio
    async def test_obo_exchange_ms_fabric(self, monkeypatch):
        """OBO exchange sends correct scope for MS Fabric."""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = dict(urllib.parse.parse_qsl(request.content.decode()))
            return httpx.Response(200, json={
                "access_token": "obo_fabric",
                "expires_in": 3600,
            })

        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient
        class _Patched(original):
            def __init__(self, *a, **kw):
                kw["transport"] = transport
                super().__init__(*a, **kw)
        monkeypatch.setattr(httpx, "AsyncClient", _Patched)

        conn = _make_connection(
            type="ms_fabric",
            credentials={"tenant_id": "t2", "client_id": "c2", "client_secret": "s2"},
        )
        tokens = await exchange_obo_token("login_token", conn)
        assert tokens["access_token"] == "obo_fabric"
        # Mirrors the delegated scope above: the SQL endpoint needs an Azure
        # SQL audience, so _OBO_SCOPES requests user_impersonation too.
        assert "database.windows.net/user_impersonation" in captured["body"]["scope"]

    @pytest.mark.asyncio
    async def test_obo_exchange_uses_oauth_client_fallback(self, monkeypatch):
        """OBO prefers oauth_client_id over client_id."""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = dict(urllib.parse.parse_qsl(request.content.decode()))
            return httpx.Response(200, json={
                "access_token": "obo_at",
                "expires_in": 3600,
            })

        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient
        class _Patched(original):
            def __init__(self, *a, **kw):
                kw["transport"] = transport
                super().__init__(*a, **kw)
        monkeypatch.setattr(httpx, "AsyncClient", _Patched)

        conn = _make_connection(
            type="powerbi",
            credentials={
                "tenant_id": "t1",
                "client_id": "c1", "client_secret": "s1",
                "oauth_client_id": "oc1", "oauth_client_secret": "os1",
            },
        )
        await exchange_obo_token("login_token", conn)
        assert captured["body"]["client_id"] == "oc1"
        assert captured["body"]["client_secret"] == "os1"

    @pytest.mark.asyncio
    async def test_obo_unsupported_type_raises(self):
        """OBO raises ValueError for non-Entra connection types."""
        conn = _make_connection(type="bigquery", credentials={})
        with pytest.raises(ValueError, match="OBO not supported"):
            await exchange_obo_token("login_token", conn)

    @pytest.mark.asyncio
    async def test_obo_missing_tenant_raises(self):
        """OBO raises ValueError when tenant_id is missing."""
        conn = _make_connection(
            type="powerbi",
            credentials={"client_id": "c1", "client_secret": "s1"},
        )
        with pytest.raises(ValueError, match="tenant_id"):
            await exchange_obo_token("login_token", conn)

    @pytest.mark.asyncio
    async def test_obo_exchange_error(self, monkeypatch):
        """OBO raises ValueError on HTTP error from Entra."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "AADSTS..."})

        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient
        class _Patched(original):
            def __init__(self, *a, **kw):
                kw["transport"] = transport
                super().__init__(*a, **kw)
        monkeypatch.setattr(httpx, "AsyncClient", _Patched)

        conn = _make_connection(
            type="powerbi",
            credentials={"tenant_id": "t1", "client_id": "c1", "client_secret": "s1"},
        )
        with pytest.raises(ValueError, match="OBO token exchange failed"):
            await exchange_obo_token("login_token", conn)

    def test_entra_obo_connection_types(self):
        """Only Microsoft connection types support OBO."""
        assert "powerbi" in ENTRA_OBO_CONNECTION_TYPES
        assert "ms_fabric" in ENTRA_OBO_CONNECTION_TYPES
        assert "bigquery" not in ENTRA_OBO_CONNECTION_TYPES


# ---------------------------------------------------------------------------
# _is_entra_provider Tests
# ---------------------------------------------------------------------------

class TestIsEntraProvider:
    def test_entra_provider(self):
        """Detects Entra ID from issuer URL."""
        from app.services.auth_providers import _is_entra_provider
        mock_cfg = MagicMock()
        mock_cfg.issuer = "https://login.microsoftonline.com/tenant-id/v2.0"
        with patch("app.services.auth_providers._get_oidc_config", return_value=mock_cfg):
            assert _is_entra_provider("entra_id") is True

    def test_sts_windows_provider(self):
        """Detects Entra ID from sts.windows.net issuer."""
        from app.services.auth_providers import _is_entra_provider
        mock_cfg = MagicMock()
        mock_cfg.issuer = "https://sts.windows.net/tenant-id/"
        with patch("app.services.auth_providers._get_oidc_config", return_value=mock_cfg):
            assert _is_entra_provider("entra_id") is True

    def test_non_entra_provider(self):
        """Non-Entra OIDC providers return False."""
        from app.services.auth_providers import _is_entra_provider
        mock_cfg = MagicMock()
        mock_cfg.issuer = "https://accounts.google.com"
        with patch("app.services.auth_providers._get_oidc_config", return_value=mock_cfg):
            assert _is_entra_provider("google") is False

    def test_unknown_provider(self):
        """Unknown provider returns False."""
        from app.services.auth_providers import _is_entra_provider
        with patch("app.services.auth_providers._get_oidc_config", return_value=None):
            assert _is_entra_provider("nonexistent") is False


# ---------------------------------------------------------------------------
# parse_expires_at Tests
# ---------------------------------------------------------------------------

class TestParseExpiresAt:
    """parse_expires_at must return naive UTC so the value is storable on
    PostgreSQL TIMESTAMP WITHOUT TIME ZONE columns (regression: asyncpg
    DataError 'can't subtract offset-naive and offset-aware datetimes')."""

    def test_aware_iso_string_becomes_naive_utc(self):
        # The shape produced by exchange_*_token(): RFC3339 with +00:00 offset.
        result = parse_expires_at("2026-06-02T10:09:32.274348+00:00")
        assert result.tzinfo is None
        assert result == datetime(2026, 6, 2, 10, 9, 32, 274348)

    def test_offset_is_converted_to_utc_then_stripped(self):
        # A +02:00 wall-clock time must be shifted to 08:00 UTC, then made naive.
        result = parse_expires_at("2026-06-02T10:00:00+02:00")
        assert result.tzinfo is None
        assert result == datetime(2026, 6, 2, 8, 0, 0)

    def test_naive_iso_string_passthrough(self):
        result = parse_expires_at("2026-06-02T10:00:00")
        assert result.tzinfo is None
        assert result == datetime(2026, 6, 2, 10, 0, 0)

    def test_none_returns_none(self):
        assert parse_expires_at(None) is None

    def test_empty_string_returns_none(self):
        assert parse_expires_at("") is None

    def test_real_token_expires_at_is_naive(self):
        # End-to-end with the exact producer expression.
        expires_at = datetime.fromtimestamp(time.time() + 3600, tz=timezone.utc).isoformat()
        result = parse_expires_at(expires_at)
        assert result.tzinfo is None
