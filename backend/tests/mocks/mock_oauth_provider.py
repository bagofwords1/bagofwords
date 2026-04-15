"""
Lightweight mock OAuth provider for testing.
Instead of hitting real Entra ID / Google OAuth endpoints, this provides
a MockOAuthProvider that can be injected via monkeypatch/patch to replace
the real OAuth flows.

Usage in E2E tests:
    from tests.mocks.mock_oauth_provider import MockOAuthProvider, patch_oauth_for_tests

    with patch_oauth_for_tests():
        # All OAuth routes will use the mock provider
        response = test_client.get(f"/api/connections/{conn_id}/oauth/authorize", ...)
"""
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
from unittest.mock import patch


class MockOAuthProvider:
    """
    In-process mock that replaces real OAuth provider interactions.
    No network calls — pure Python.

    Tracks issued codes, exchanged tokens, refresh calls, and OBO exchanges
    for test assertions.
    """

    # Default OAuth configs per connection type
    PROVIDER_CONFIGS = {
        "powerbi": {
            "authorize_url": "https://mock-oauth.test/microsoft/authorize",
            "token_url": "https://mock-oauth.test/microsoft/token",
            "client_id": "mock_ms_client_id",
            "client_secret": "mock_ms_client_secret",
            "scopes": "https://analysis.windows.net/powerbi/api/.default offline_access",
            "provider_name": "microsoft",
        },
        "ms_fabric": {
            "authorize_url": "https://mock-oauth.test/microsoft/authorize",
            "token_url": "https://mock-oauth.test/microsoft/token",
            "client_id": "mock_ms_client_id",
            "client_secret": "mock_ms_client_secret",
            "scopes": "https://api.fabric.microsoft.com/.default offline_access",
            "provider_name": "microsoft",
        },
        "bigquery": {
            "authorize_url": "https://mock-oauth.test/google/authorize",
            "token_url": "https://mock-oauth.test/google/token",
            "client_id": "mock_google_client_id",
            "client_secret": "mock_google_client_secret",
            "scopes": "https://www.googleapis.com/auth/bigquery.readonly offline_access",
            "provider_name": "google",
        },
    }

    def __init__(self):
        self._issued_codes: Dict[str, Dict[str, Any]] = {}
        self._token_counter: int = 0
        self._exchange_log: List[Dict[str, Any]] = []
        self._refresh_log: List[Dict[str, Any]] = []
        self._obo_log: List[Dict[str, Any]] = []
        # Control OBO behavior per connection type (set to Exception to simulate failure)
        self._obo_failures: Dict[str, Exception] = {}

    def get_oauth_params(self, connection) -> dict:
        """Mock replacement for connection_oauth_service.get_oauth_params()."""
        conn_type = connection.type
        if conn_type not in self.PROVIDER_CONFIGS:
            raise ValueError(f"OAuth not supported for connection type: {conn_type}")
        return dict(self.PROVIDER_CONFIGS[conn_type])

    def issue_code(self, connection_id: str, code_challenge: str = None) -> str:
        """Issue a mock authorization code (simulates what the OAuth provider does)."""
        code = f"mock_code_{secrets.token_urlsafe(8)}"
        self._issued_codes[code] = {
            "connection_id": connection_id,
            "code_challenge": code_challenge,
            "issued_at": time.time(),
        }
        return code

    async def exchange_code_for_tokens(
        self,
        oauth_params: dict,
        code: str,
        redirect_uri: str,
        code_verifier: Optional[str] = None,
    ) -> dict:
        """Mock replacement for connection_oauth_service.exchange_code_for_tokens()."""
        self._token_counter += 1
        self._exchange_log.append({
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "client_id": oauth_params.get("client_id"),
        })
        return self._make_token_response(f"access_token_v{self._token_counter}")

    async def refresh_access_token(
        self,
        oauth_params: dict,
        refresh_token: str,
    ) -> dict:
        """Mock replacement for connection_oauth_service.refresh_access_token()."""
        self._token_counter += 1
        self._refresh_log.append({
            "refresh_token": refresh_token,
            "client_id": oauth_params.get("client_id"),
        })
        return self._make_token_response(f"refreshed_token_v{self._token_counter}")

    async def exchange_obo_token(
        self,
        login_access_token: str,
        connection,
    ) -> dict:
        """Mock replacement for connection_oauth_service.exchange_obo_token()."""
        conn_type = connection.type

        # Check for programmed failures
        if conn_type in self._obo_failures:
            raise self._obo_failures[conn_type]

        self._token_counter += 1
        self._obo_log.append({
            "login_access_token": login_access_token,
            "connection_id": connection.id,
            "connection_type": conn_type,
        })
        return self._make_token_response(f"obo_token_v{self._token_counter}")

    def set_obo_failure(self, connection_type: str, error: Exception):
        """Configure OBO to fail for a specific connection type."""
        self._obo_failures[connection_type] = error

    def clear_obo_failures(self):
        """Remove all programmed OBO failures."""
        self._obo_failures.clear()

    def _make_token_response(self, access_token: str) -> dict:
        return {
            "access_token": access_token,
            "refresh_token": f"refresh_{secrets.token_urlsafe(8)}",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "token_type": "Bearer",
        }

    # -- Assertion helpers for tests --

    @property
    def exchange_log(self) -> List[Dict[str, Any]]:
        """All token exchange calls made."""
        return self._exchange_log

    @property
    def refresh_log(self) -> List[Dict[str, Any]]:
        """All token refresh calls made."""
        return self._refresh_log

    @property
    def obo_log(self) -> List[Dict[str, Any]]:
        """All OBO token exchange calls made."""
        return self._obo_log

    @property
    def total_tokens_issued(self) -> int:
        return self._token_counter


def patch_oauth_for_tests(mock: MockOAuthProvider = None):
    """Context manager that patches all OAuth service functions with the mock.

    Usage:
        mock = MockOAuthProvider()
        with patch_oauth_for_tests(mock):
            # routes use mock.get_oauth_params, mock.exchange_code_for_tokens, etc.
            ...
        assert mock.total_tokens_issued == 1
    """
    if mock is None:
        mock = MockOAuthProvider()

    return _CombinedPatch(mock)


def _async_mock(*args, **kwargs):
    from unittest.mock import AsyncMock
    return AsyncMock(return_value=[])


class _CombinedPatch:
    """Applies all OAuth-related patches as a single context manager."""

    def __init__(self, mock: MockOAuthProvider):
        self.mock = mock
        self._patches = [
            patch(
                "app.routes.connection_oauth.get_oauth_params",
                side_effect=mock.get_oauth_params,
            ),
            patch(
                "app.routes.connection_oauth.exchange_code_for_tokens",
                side_effect=mock.exchange_code_for_tokens,
            ),
            patch(
                "app.services.connection_oauth_service.refresh_access_token",
                side_effect=mock.refresh_access_token,
            ),
            patch(
                "app.services.connection_oauth_service.exchange_obo_token",
                side_effect=mock.exchange_obo_token,
            ),
            patch(
                "app.services.data_source_service.DataSourceService.get_user_data_source_schema",
                new_callable=lambda: _async_mock,
            ),
        ]

    def __enter__(self):
        for p in self._patches:
            p.__enter__()
        return self.mock

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.__exit__(*exc)
