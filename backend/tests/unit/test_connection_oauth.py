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
