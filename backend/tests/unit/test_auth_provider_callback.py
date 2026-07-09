"""
Unit tests for OAuth/OIDC callback error handling in auth_providers.

Focus: when a provider (e.g. Entra) redirects back to the callback with an
``error``/``error_description`` instead of a ``code``, the real provider error
must be surfaced on the sign-in page rather than a misleading "Missing
code/state".
"""
import urllib.parse
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import auth_providers


def _make_request(query: dict):
    request = MagicMock()
    request.query_params = query
    request.cookies = {}
    request.headers = {}
    request.client = MagicMock(host="127.0.0.1")
    return request


def _redirect_error(response) -> str:
    location = response.headers["location"]
    parsed = urllib.parse.urlparse(location)
    params = urllib.parse.parse_qs(parsed.query)
    return params.get("error", [None])[0]


@pytest.mark.asyncio
async def test_callback_surfaces_provider_error_description():
    request = _make_request(
        {
            "error": "invalid_resource",
            "error_description": (
                "AADSTS500011: The resource principal named "
                "api://428306b2-a28d-485b-9ac4-65eff1572594 was not found in the tenant."
            ),
            "state": "1db1863923554d09b753106de442202d",
        }
    )

    with patch.object(auth_providers, "_audit_auth_event", new=AsyncMock()):
        response = await auth_providers.handle_callback(
            provider="entra",
            request=request,
            code=None,
            state="1db1863923554d09b753106de442202d",
            user_manager=MagicMock(),
        )

    assert response.status_code == 303
    error = _redirect_error(response)
    assert "AADSTS500011" in error
    assert "invalid_resource" in error
    # The old, misleading message must no longer be shown.
    assert error != "Missing code/state"


@pytest.mark.asyncio
async def test_callback_error_without_description_falls_back_to_error_code():
    request = _make_request({"error": "access_denied", "state": "abc"})

    with patch.object(auth_providers, "_audit_auth_event", new=AsyncMock()):
        response = await auth_providers.handle_callback(
            provider="entra",
            request=request,
            code=None,
            state="abc",
            user_manager=MagicMock(),
        )

    error = _redirect_error(response)
    assert "access_denied" in error
    assert error != "Missing code/state"


@pytest.mark.asyncio
async def test_callback_still_reports_missing_code_when_no_provider_error():
    request = _make_request({})

    with patch.object(auth_providers, "_audit_auth_event", new=AsyncMock()):
        response = await auth_providers.handle_callback(
            provider="entra",
            request=request,
            code=None,
            state=None,
            user_manager=MagicMock(),
        )

    assert _redirect_error(response) == "Missing code/state"
