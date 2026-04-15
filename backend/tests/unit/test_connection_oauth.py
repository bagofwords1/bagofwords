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
        assert "database.windows.net" in params["scopes"]

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
        assert "database.windows.net" in captured["body"]["scope"]

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
