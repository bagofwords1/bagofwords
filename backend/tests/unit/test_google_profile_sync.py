"""
Unit tests for the Google profile-sync service.

The Google sync merges two sources under the login-granted openid/profile/email
scopes: the OAuth2 userinfo endpoint (display name, locale, hosted domain) and
the People API (Workspace directory job title/department/organization). The
People API layer must degrade gracefully when the API isn't enabled on the
OAuth client's GCP project (403), and an expired token with no refresh path
must surface as GoogleReauthRequired. No database or network — collaborators
are patched, HTTP goes through httpx.MockTransport.
"""
import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

from app.ee.oidc import google_profile_service
from app.ee.oidc.google_profile_service import (
    GoogleReauthRequired,
    _flatten_people,
    _flatten_userinfo,
    _resolve_google_profile,
    fetch_profile,
    fetch_profile_fields,
)


USERINFO = {
    "id": "112432357396918455542",
    "email": "bow@bagofwords.com",
    "name": "Bow Demo",
    "locale": "en",
    "hd": "bagofwords.com",
}

PERSON = {
    "names": [{"displayName": "Bow Demo"}],
    "organizations": [
        {"metadata": {"primary": True}, "name": "Bag of Words", "title": "Data Analyst", "department": "Analytics"},
        {"name": "Old Corp", "title": "Intern"},
    ],
    "locations": [{"metadata": {"primary": True}, "buildingId": "TLV-1", "floor": "3"}],
    "locales": [{"value": "en-US"}],
}


def _transport(userinfo_status=200, people_status=200, userinfo=None, person=None):
    """MockTransport serving the userinfo and People endpoints."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "googleapis.com/oauth2/v1/userinfo" in str(request.url):
            return httpx.Response(userinfo_status, json=userinfo or USERINFO)
        if "people.googleapis.com" in str(request.url):
            return httpx.Response(people_status, json=person or PERSON)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def _patched_client(transport):
    original = httpx.AsyncClient

    class _Patched(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    return _Patched


def _fake_account(token: str = "login-token", refresh: str = None) -> MagicMock:
    acc = MagicMock()
    acc.oauth_name = "google"
    acc.account_email = "bow@bagofwords.com"
    acc.access_token = token
    acc.refresh_token = refresh
    acc.expires_at = None
    return acc


# ── Flattening ─────────────────────────────────────────────────────────────

def test_flatten_people_prefers_primary_organization():
    out = _flatten_people(PERSON)
    assert out["jobTitle"] == "Data Analyst"
    assert out["department"] == "Analytics"
    assert out["organization"] == "Bag of Words"
    assert out["displayName"] == "Bow Demo"
    assert out["locale"] == "en-US"
    assert out["location"] == "TLV-1, 3"  # composed from building/floor


def test_flatten_people_org_location_wins_over_locations():
    person = {
        "organizations": [{"title": "Eng", "location": "Tel Aviv HQ"}],
        "locations": [{"buildingId": "B2"}],
    }
    assert _flatten_people(person)["location"] == "Tel Aviv HQ"


def test_flatten_userinfo_maps_allowlisted_names():
    out = _flatten_userinfo(USERINFO)
    assert out == {"displayName": "Bow Demo", "locale": "en", "hostedDomain": "bagofwords.com"}


# ── _resolve_google_profile ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_merges_userinfo_and_people():
    fields = ["displayName", "jobTitle", "department", "organization", "locale", "hostedDomain"]
    with patch.object(httpx, "AsyncClient", _patched_client(_transport())):
        out = await _resolve_google_profile("tok", fields)
    # People values win for overlapping keys; hostedDomain only exists in userinfo.
    assert out == {
        "displayName": "Bow Demo",
        "jobTitle": "Data Analyst",
        "department": "Analytics",
        "organization": "Bag of Words",
        "locale": "en-US",
        "hostedDomain": "bagofwords.com",
    }


@pytest.mark.asyncio
async def test_resolve_degrades_to_userinfo_when_people_api_disabled():
    """403 from the People API (SERVICE_DISABLED) → userinfo-only fields, no raise."""
    fields = ["displayName", "jobTitle", "hostedDomain"]
    with patch.object(httpx, "AsyncClient", _patched_client(_transport(people_status=403))):
        out = await _resolve_google_profile("tok", fields)
    assert out == {"displayName": "Bow Demo", "jobTitle": None, "hostedDomain": "bagofwords.com"}


@pytest.mark.asyncio
async def test_resolve_skips_people_call_for_userinfo_only_fields():
    """hostedDomain alone never touches the People API."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "userinfo" in str(request.url):
            return httpx.Response(200, json=USERINFO)
        return httpx.Response(500)

    with patch.object(httpx, "AsyncClient", _patched_client(httpx.MockTransport(handler))):
        out = await _resolve_google_profile("tok", ["hostedDomain"])
    assert out == {"hostedDomain": "bagofwords.com"}
    assert all("people.googleapis" not in u for u in calls)


@pytest.mark.asyncio
async def test_resolve_userinfo_401_propagates():
    """userinfo 401 (expired token) must propagate so the caller can refresh."""
    with patch.object(httpx, "AsyncClient", _patched_client(_transport(userinfo_status=401))):
        with pytest.raises(httpx.HTTPStatusError):
            await _resolve_google_profile("tok", ["displayName"])


# ── fetch_profile_fields fallback chain ────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_profile_fields_refreshes_on_401():
    """401 → refresh-token exchange → retry succeeds."""
    seen_tokens = []

    async def fake_resolve(token, fields):
        seen_tokens.append(token)
        if token == "stale":
            raise httpx.HTTPStatusError(
                "401",
                request=httpx.Request("GET", google_profile_service.USERINFO_URL),
                response=httpx.Response(401, request=httpx.Request("GET", google_profile_service.USERINFO_URL)),
            )
        return {"displayName": "Bow Demo"}

    acc = _fake_account("stale", refresh="rt")
    with patch.object(google_profile_service, "_resolve_google_profile", side_effect=fake_resolve), \
         patch.object(google_profile_service, "_google_oauth_account", AsyncMock(return_value=acc)), \
         patch.object(google_profile_service, "_refresh_google_token", AsyncMock(return_value="fresh")):
        out = await fetch_profile_fields(
            MagicMock(), MagicMock(id="u1"), ["displayName"], access_token="stale"
        )
    assert out == {"displayName": "Bow Demo"}
    assert seen_tokens == ["stale", "fresh"]


@pytest.mark.asyncio
async def test_fetch_profile_fields_raises_reauth_when_refresh_fails():
    """401 and no refresh path (default Google login has no refresh token) → reauth."""
    err = httpx.HTTPStatusError(
        "401",
        request=httpx.Request("GET", google_profile_service.USERINFO_URL),
        response=httpx.Response(401, request=httpx.Request("GET", google_profile_service.USERINFO_URL)),
    )
    with patch.object(google_profile_service, "_resolve_google_profile", AsyncMock(side_effect=err)), \
         patch.object(google_profile_service, "_google_oauth_account", AsyncMock(return_value=_fake_account())), \
         patch.object(google_profile_service, "_refresh_google_token", AsyncMock(return_value=None)):
        with pytest.raises(GoogleReauthRequired):
            await fetch_profile_fields(
                MagicMock(), MagicMock(id="u1"), ["displayName"], access_token="stale"
            )


@pytest.mark.asyncio
async def test_fetch_profile_fields_non_401_propagates():
    """Only 401 triggers the refresh fallback — other errors surface as-is."""
    err = httpx.HTTPStatusError(
        "500",
        request=httpx.Request("GET", google_profile_service.USERINFO_URL),
        response=httpx.Response(500, request=httpx.Request("GET", google_profile_service.USERINFO_URL)),
    )
    refresh = AsyncMock()
    with patch.object(google_profile_service, "_resolve_google_profile", AsyncMock(side_effect=err)), \
         patch.object(google_profile_service, "_google_oauth_account", AsyncMock(return_value=_fake_account())), \
         patch.object(google_profile_service, "_refresh_google_token", refresh):
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_profile_fields(
                MagicMock(), MagicMock(id="u1"), ["displayName"], access_token="tok"
            )
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_profile_fields_returns_empty_without_login():
    """No Google login on file (and no explicit token) → {} rather than an error."""
    with patch.object(google_profile_service, "_google_oauth_account", AsyncMock(return_value=None)), \
         patch.object(google_profile_service, "get_google_access_token", AsyncMock(return_value=None)):
        out = await fetch_profile_fields(MagicMock(), MagicMock(id="u1"), ["displayName"])
    assert out == {}


@pytest.mark.asyncio
async def test_fetch_profile_drops_empty_values():
    with patch.object(
        google_profile_service,
        "fetch_profile_fields",
        AsyncMock(return_value={"displayName": "Bow", "jobTitle": None, "department": ""}),
    ):
        out = await fetch_profile(MagicMock(), MagicMock(id="u1"), ["displayName", "jobTitle", "department"])
    assert out == {"displayName": "Bow"}


# ── provider detection ─────────────────────────────────────────────────────

def test_is_google_provider_builtin_and_oidc_issuer():
    from app.services.auth_providers import _is_google_provider

    assert _is_google_provider("google") is True

    cfg = MagicMock(issuer="https://accounts.google.com")
    with patch("app.services.auth_providers._get_oidc_config", return_value=cfg):
        assert _is_google_provider("google-oidc") is True

    entra = MagicMock(issuer="https://login.microsoftonline.com/tid/v2.0")
    with patch("app.services.auth_providers._get_oidc_config", return_value=entra):
        assert _is_google_provider("entra") is False

    with patch("app.services.auth_providers._get_oidc_config", return_value=None):
        assert _is_google_provider("unknown") is False
