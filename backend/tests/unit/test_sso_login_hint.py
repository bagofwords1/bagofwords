"""Unit tests for the `login_hint` pass-through on the SSO authorize URL.

An app embedding BOW already knows which user it is opening the session for.
Naming that user on the authorize request is what keeps the provider from
stopping on an account picker, which is the difference between a silent SSO
round trip and a visible one.
"""
from unittest.mock import patch

import pytest
from starlette.requests import Request

from app.services.auth_providers import _read_login_hint, build_authorize_url


def _FakeRequest(query: str = "") -> Request:
    """A real Request so base-URL derivation behaves as it does in production."""
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/api/auth/entra/authorize",
        "raw_path": b"/api/auth/entra/authorize",
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(b"host", b"bow.example.com")],
        "server": ("bow.example.com", 443),
        "client": ("10.0.0.1", 1234),
    })


# ── _read_login_hint ──


def test_reads_a_plain_hint():
    assert _read_login_hint(_FakeRequest("login_hint=alice@example.com")) == (
        "alice@example.com"
    )


def test_absent_hint_is_none():
    assert _read_login_hint(_FakeRequest("")) is None


def test_blank_hint_is_none():
    assert _read_login_hint(_FakeRequest("login_hint=%20%20")) is None


def test_hint_is_trimmed():
    assert _read_login_hint(_FakeRequest("login_hint=%20alice@example.com%20")) == (
        "alice@example.com"
    )


@pytest.mark.parametrize("injected", ["a@b.com%0aSet-Cookie:%20x", "a@b.com%0d%0aX:%20y"])
def test_crlf_bearing_hint_is_rejected(injected):
    """The hint is echoed into a redirect; a bare CR/LF must not survive."""
    assert _read_login_hint(_FakeRequest(f"login_hint={injected}")) is None


def test_overlong_hint_is_rejected():
    assert _read_login_hint(_FakeRequest(f"login_hint={'a' * 400}@b.com")) is None


def test_hint_at_the_length_cap_is_kept():
    hint = "a" * 320
    assert _read_login_hint(_FakeRequest(f"login_hint={hint}")) == hint


# ── build_authorize_url ──


class _StubOIDCConfig:
    name = "entra"
    enabled = True
    issuer = "https://login.microsoftonline.com/tenant-id/v2.0"
    client_id = "client-id"
    client_secret = "client-secret"
    scopes = ["openid", "profile", "email"]
    pkce = True
    redirect_path = None
    extra_authorize_params: dict = {}


async def _authorize_extras(query: str, cfg=None):
    """Run build_authorize_url and return the extras_params it passed through."""
    captured = {}

    class _StubClient:
        def __init__(self, *a, **kw):
            pass

        async def get_authorization_url(self, **kwargs):
            captured.update(kwargs)
            return "https://login.microsoftonline.com/tenant-id/oauth2/v2.0/authorize?x=1"

    with patch("app.services.auth_providers._get_oidc_config", return_value=cfg or _StubOIDCConfig()), \
         patch("app.services.auth_providers.OpenID", _StubClient):
        await build_authorize_url("entra", _FakeRequest(query))

    return captured["extras_params"]


@pytest.mark.asyncio
async def test_hint_reaches_the_provider_authorize_url():
    extras = await _authorize_extras("login_hint=alice@example.com")
    assert extras["login_hint"] == "alice@example.com"


@pytest.mark.asyncio
async def test_no_hint_sends_no_login_hint_param():
    """Without a hint the URL must look exactly as it did before this feature."""
    extras = await _authorize_extras("")
    assert "login_hint" not in extras


@pytest.mark.asyncio
async def test_pkce_is_still_applied_alongside_a_hint():
    extras = await _authorize_extras("login_hint=alice@example.com")
    assert extras["code_challenge_method"] == "S256"
    assert extras["code_challenge"]


@pytest.mark.asyncio
async def test_request_hint_beats_the_static_config_default():
    """A per-request hint names the real user, so it wins over config."""

    class _WithDefaultHint(_StubOIDCConfig):
        extra_authorize_params = {"login_hint": "shared@example.com", "domain_hint": "example"}

    extras = await _authorize_extras("login_hint=bob@example.com", cfg=_WithDefaultHint())
    assert extras["login_hint"] == "bob@example.com"
    # Unrelated configured params are untouched.
    assert extras["domain_hint"] == "example"


@pytest.mark.asyncio
async def test_configured_hint_survives_when_the_request_carries_none():
    class _WithDefaultHint(_StubOIDCConfig):
        extra_authorize_params = {"login_hint": "shared@example.com"}

    extras = await _authorize_extras("", cfg=_WithDefaultHint())
    assert extras["login_hint"] == "shared@example.com"


@pytest.mark.asyncio
async def test_rejected_hint_does_not_reach_the_provider():
    extras = await _authorize_extras("login_hint=a@b.com%0aSet-Cookie:%20x")
    assert "login_hint" not in extras
