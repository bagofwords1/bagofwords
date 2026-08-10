"""
Unit tests for the Entra profile-sync OBO fallback.

Customer Entra configs request an api://<client-id>/access_as_user scope at
login (needed for data-source OBO), so the stored login token's audience is the
app's own API and Graph /me rejects it with 401. The profile service must fall
back to an On-Behalf-Of exchange for a Graph User.Read token, persist it, and
retry. No database or network — collaborators are patched.
"""
import base64
import json

import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

from app.ee.oidc import profile_service
from app.ee.oidc.profile_service import (
    EntraReauthRequired,
    fetch_profile,
    fetch_profile_fields,
    _is_graph_audience,
    _obo_exchange_for_graph,
)


def _jwt(aud) -> str:
    """Unsigned JWT carrying just the audience claim (payload decode only)."""
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{seg({'alg': 'RS256'})}.{seg({'aud': aud})}.sig"


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://graph.microsoft.com/v1.0/me")
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=req, response=httpx.Response(status, request=req)
    )


def _fake_account(token: str = "login-token") -> MagicMock:
    acc = MagicMock()
    acc.oauth_name = "entra"
    acc.access_token = token
    acc.refresh_token = None
    acc.expires_at = None
    return acc


@pytest.mark.asyncio
async def test_fetch_profile_falls_back_to_obo_on_401():
    """Graph 401 (wrong-audience login token) → OBO exchange → retry succeeds."""
    calls = []

    async def fake_resolve(token, fields):
        calls.append(token)
        if token == "login-token":
            raise _http_status_error(401)
        return {"jobTitle": "hello world", "department": None}

    obo_response = {"access_token": "graph-token", "refresh_token": "rt", "expires_in": 3600}
    persist = AsyncMock()

    with patch.object(profile_service, "resolve_user_profile", side_effect=fake_resolve), \
         patch.object(profile_service, "_entra_oauth_account", AsyncMock(return_value=_fake_account())), \
         patch.object(profile_service, "_obo_exchange_for_graph", AsyncMock(return_value=obo_response)), \
         patch.object(profile_service, "_persist_graph_tokens", persist):
        result = await fetch_profile(
            db=MagicMock(), user=MagicMock(id="u1"),
            fields=["jobTitle", "department"], access_token="login-token",
        )

    assert result == {"jobTitle": "hello world"}  # empty values dropped
    assert calls == ["login-token", "graph-token"]
    persist.assert_awaited_once()
    assert persist.await_args.args[2] == obo_response


@pytest.mark.asyncio
async def test_fetch_profile_fields_uses_stored_token_as_assertion():
    """Without an explicit token (preview path), the stored token is the OBO assertion."""
    obo = AsyncMock(return_value={"access_token": "graph-token", "expires_in": 3600})

    async def fake_resolve(token, fields):
        if token == "stored-token":
            raise _http_status_error(401)
        return {"jobTitle": "x"}

    with patch.object(profile_service, "resolve_user_profile", side_effect=fake_resolve), \
         patch.object(profile_service, "_entra_oauth_account", AsyncMock(return_value=_fake_account("stored-token"))), \
         patch.object(profile_service, "get_entra_graph_token", AsyncMock(return_value="stored-token")), \
         patch.object(profile_service, "_obo_exchange_for_graph", obo), \
         patch.object(profile_service, "_persist_graph_tokens", AsyncMock()):
        result = await fetch_profile_fields(MagicMock(), MagicMock(id="u1"), ["jobTitle"])

    assert result == {"jobTitle": "x"}
    obo.assert_awaited_once_with("entra", "stored-token")


@pytest.mark.asyncio
async def test_fetch_profile_raises_reauth_when_obo_fails():
    """401 and a failed OBO exchange (expired assertion, no consent) → EntraReauthRequired."""
    with patch.object(profile_service, "resolve_user_profile", AsyncMock(side_effect=_http_status_error(401))), \
         patch.object(profile_service, "_entra_oauth_account", AsyncMock(return_value=_fake_account())), \
         patch.object(profile_service, "_obo_exchange_for_graph", AsyncMock(return_value=None)):
        with pytest.raises(EntraReauthRequired):
            await fetch_profile_fields(
                MagicMock(), MagicMock(id="u1"), ["jobTitle"], access_token="login-token"
            )


@pytest.mark.asyncio
async def test_fetch_profile_non_401_errors_propagate():
    """Only 401 triggers the OBO fallback — other Graph errors surface as-is."""
    obo = AsyncMock()
    with patch.object(profile_service, "resolve_user_profile", AsyncMock(side_effect=_http_status_error(500))), \
         patch.object(profile_service, "_entra_oauth_account", AsyncMock(return_value=_fake_account())), \
         patch.object(profile_service, "_obo_exchange_for_graph", obo):
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_profile_fields(
                MagicMock(), MagicMock(id="u1"), ["jobTitle"], access_token="login-token"
            )
    obo.assert_not_awaited()


@pytest.mark.parametrize("aud, expected", [
    ("https://graph.microsoft.com", True),
    ("https://graph.microsoft.com/", True),
    ("00000003-0000-0000-c000-000000000000", True),
    (["https://graph.microsoft.com"], True),
    ("api://cid", False),
    ("cid", False),
])
def test_is_graph_audience(aud, expected):
    assert _is_graph_audience(_jwt(aud)) is expected


def test_is_graph_audience_tolerates_opaque_tokens():
    """Unparsable tokens fall through to False so the OBO is still attempted."""
    assert _is_graph_audience("not-a-jwt") is False
    assert _is_graph_audience("a.b.c") is False


@pytest.mark.asyncio
async def test_graph_audience_token_is_not_resubmitted_as_assertion():
    """A persisted Graph token 401s → re-auth, not an AADSTS50013 OBO attempt."""
    graph_token = _jwt("https://graph.microsoft.com")
    obo = AsyncMock()

    with patch.object(profile_service, "resolve_user_profile", AsyncMock(side_effect=_http_status_error(401))), \
         patch.object(profile_service, "_entra_oauth_account", AsyncMock(return_value=_fake_account(graph_token))), \
         patch.object(profile_service, "get_entra_graph_token", AsyncMock(return_value=graph_token)), \
         patch.object(profile_service, "_obo_exchange_for_graph", obo):
        with pytest.raises(EntraReauthRequired):
            await fetch_profile_fields(MagicMock(), MagicMock(id="u1"), ["jobTitle"])

    obo.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_audience_login_token_still_exchanges():
    """The api://-scoped login token remains a valid assertion."""
    login_token = _jwt("api://cid")
    obo = AsyncMock(return_value={"access_token": "graph-token", "expires_in": 3600})

    async def fake_resolve(token, fields):
        if token == login_token:
            raise _http_status_error(401)
        return {"jobTitle": "x"}

    with patch.object(profile_service, "resolve_user_profile", side_effect=fake_resolve), \
         patch.object(profile_service, "_entra_oauth_account", AsyncMock(return_value=_fake_account(login_token))), \
         patch.object(profile_service, "_obo_exchange_for_graph", obo), \
         patch.object(profile_service, "_persist_graph_tokens", AsyncMock()):
        result = await fetch_profile_fields(
            MagicMock(), MagicMock(id="u1"), ["jobTitle"], access_token=login_token
        )

    assert result == {"jobTitle": "x"}
    obo.assert_awaited_once_with("entra", login_token)


@pytest.mark.asyncio
async def test_obo_exchange_posts_jwt_bearer_grant():
    """The OBO request carries the jwt-bearer grant, assertion, and Graph scope."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        form = dict(pair.split("=", 1) for pair in request.content.decode().split("&"))
        seen.update(form)
        return httpx.Response(200, json={"access_token": "graph-token", "expires_in": 3599})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class _Patched(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    cfg = MagicMock(client_id="cid", client_secret="sec", issuer="https://login.microsoftonline.com/tid/v2.0")

    with patch.object(httpx, "AsyncClient", _Patched), \
         patch("app.services.auth_providers._get_oidc_config", return_value=cfg), \
         patch("app.services.auth_providers._discover_endpoints",
               AsyncMock(return_value={"token_endpoint": "https://login.microsoftonline.com/tid/oauth2/v2.0/token"})):
        token = await _obo_exchange_for_graph("entra", "assertion-token")

    assert token == {"access_token": "graph-token", "expires_in": 3599}
    assert seen["grant_type"] == "urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer"
    assert seen["assertion"] == "assertion-token"
    assert seen["requested_token_use"] == "on_behalf_of"
    assert "graph.microsoft.com%2FUser.Read" in seen["scope"]


@pytest.mark.asyncio
async def test_obo_exchange_returns_none_on_entra_error():
    """A 400 from the token endpoint (e.g. AADSTS500133 expired assertion) → None."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "AADSTS500133"})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class _Patched(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    cfg = MagicMock(client_id="cid", client_secret="sec", issuer="https://login.microsoftonline.com/tid/v2.0")

    with patch.object(httpx, "AsyncClient", _Patched), \
         patch("app.services.auth_providers._get_oidc_config", return_value=cfg), \
         patch("app.services.auth_providers._discover_endpoints",
               AsyncMock(return_value={"token_endpoint": "https://login.microsoftonline.com/tid/oauth2/v2.0/token"})):
        assert await _obo_exchange_for_graph("entra", "stale") is None
