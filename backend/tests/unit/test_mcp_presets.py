"""Unit tests for the MCP connector presets, data_shape license gate, and DCR SSRF guard."""
import pytest

from app.schemas.data_source_registry import (
    mcp_presets, mcp_preset, allowed_dcr_hosts,
)
from app.services.connection_service import (
    ConnectionService, _user_auth_needs_enterprise, _looks_like_auth_challenge,
)


def test_presets_include_default_dcr_set():
    keys = {p["key"] for p in mcp_presets()}
    assert {"monday", "notion", "atlassian", "linear", "sentry"} <= keys


def test_dcr_presets_are_oauth():
    # The zero-setup (DCR) set connects via per-user OAuth.
    for key in ("monday", "notion", "atlassian", "linear", "sentry"):
        assert mcp_preset(key).auth == "oauth"
    # GitHub/Gmail need an OAuth app.
    assert mcp_preset("github").auth == "oauth_app"
    assert mcp_preset("gmail").auth == "oauth_app"


def test_x_preset_is_bearer():
    # X has no DCR — it connects with an app-only bearer token from the
    # X Developer Portal, over streamable HTTP.
    x = mcp_preset("x")
    assert x.auth == "bearer"
    assert x.server_url == "https://api.x.com/mcp"
    assert x.transport == "streamable_http"


def test_license_gate_is_data_shape_scoped():
    # Integrations (tools/files/objects) → per-user auth is free.
    assert _user_auth_needs_enterprise("mcp") is False
    assert _user_auth_needs_enterprise("onedrive") is False
    # Warehouses/databases (tables) → Enterprise.
    assert _user_auth_needs_enterprise("postgresql") is True
    # Unknown type → conservative (gated).
    assert _user_auth_needs_enterprise("totally_unknown_type") is True


def test_dcr_allowlist_includes_preset_hosts_only():
    hosts = allowed_dcr_hosts()
    assert "mcp.notion.com" in hosts and "mcp.monday.com" in hosts
    assert "auth.atlassian.com" in hosts  # AS host differs from resource host
    assert "evil.example.com" not in hosts


# ── Preset-scoped form defaults ────────────────────────────────────────────
# oauth_app presets carry their provider OAuth constants so the connect form can
# pre-fill them (the admin only supplies client_id/secret) instead of asking for
# invariant endpoints by hand.

def test_oauth_app_presets_prefill_endpoints():
    for key in ("x", "github", "gmail", "google_drive"):
        d = mcp_preset(key).oauth_defaults
        assert d is not None, f"{key} should carry oauth_defaults"
        assert d.authorize_url and d.authorize_url.startswith("https://")
        assert d.token_url and d.token_url.startswith("https://")


def test_x_oauth_defaults_are_correct():
    d = mcp_preset("x").oauth_defaults
    assert d.authorize_url == "https://twitter.com/i/oauth2/authorize"
    assert d.token_url == "https://api.x.com/2/oauth2/token"
    # tweet.write (not the invalid twitter.write) is the X scope for posting.
    assert "tweet.write" in d.scopes
    assert "twitter.write" not in d.scopes


def test_dcr_presets_have_no_oauth_defaults():
    # DCR discovers its endpoints — no admin-entered constants needed.
    for key in ("monday", "notion", "atlassian", "linear", "sentry"):
        assert mcp_preset(key).oauth_defaults is None


def test_preset_allowed_auth_gating():
    # X's server has no DCR, so the tile must not offer it.
    assert "dcr" not in (mcp_preset("x").allowed_auth or [])
    assert "oauth_app" in mcp_preset("x").allowed_auth
    # DCR presets offer only the zero-setup sign-in path.
    assert mcp_preset("monday").allowed_auth == ["dcr"]
    # oauth_app-only presets.
    assert mcp_preset("github").allowed_auth == ["oauth_app"]


def test_scope_normalization_comma_or_space():
    # The form accepts comma- or space-separated scopes; the authorize request
    # must be space-delimited (RFC 6749).
    from app.routes.connection_oauth import _normalize_scopes
    assert _normalize_scopes("tweet.read, tweet.write, users.read") == "tweet.read tweet.write users.read"
    assert _normalize_scopes("openid profile offline_access") == "openid profile offline_access"
    assert _normalize_scopes("a,b,  c ,") == "a b c"
    assert _normalize_scopes("") == ""


def test_catalog_exposes_preset_form_spec():
    # The new fields must serialize through mcp_presets() → GET /connectors/catalog.
    x = next(p for p in mcp_presets() if p["key"] == "x")
    assert "allowed_auth" in x and "oauth_defaults" in x and "sample_tools" in x
    assert x["oauth_defaults"]["authorize_url"] == "https://twitter.com/i/oauth2/authorize"


def test_sample_tools_present():
    # X keeps a hand-curated sample (not in the Anthropic directory).
    assert "get_users_by_username" in (mcp_preset("x").sample_tools or [])
    # Directory-sourced previews for the DCR / Google presets.
    assert "search" in (mcp_preset("notion").sample_tools or [])
    assert mcp_preset("monday").sample_tools  # populated from the directory
    assert mcp_preset("linear").sample_tools and mcp_preset("gmail").sample_tools
    # GitHub isn't in the directory → no static preview.
    assert mcp_preset("github").sample_tools is None


# ── Test-connection reinterpretation for per-user OAuth MCP ─────────────────

class _FakeClient:
    """Stub MCP client: atest_connection returns a preset result, no network."""
    def __init__(self, fail_message=None):
        self._fail_message = fail_message

    async def atest_connection(self):
        if self._fail_message:
            return {"success": False, "message": self._fail_message}
        return {"success": True, "message": "ok"}

    async def alist_tools(self):
        return [{"name": "a"}, {"name": "b"}]


def test_auth_challenge_detection():
    assert _looks_like_auth_challenge("Client error '401 Unauthorized' for url ...")
    assert _looks_like_auth_challenge("403 Forbidden")
    assert not _looks_like_auth_challenge("Name or service not known")
    assert not _looks_like_auth_challenge(None)


@pytest.mark.asyncio
async def test_oauth_mcp_test_treats_auth_challenge_as_pass(monkeypatch):
    # oauth_app has no token at config time; a 401 means "reachable, needs
    # sign-in" — the healthy state, so the test should PASS.
    svc = ConnectionService()
    fake = _FakeClient(fail_message="Failed to connect to MCP server: Client error '401 Unauthorized' for url 'https://api.x.com/mcp'")
    monkeypatch.setattr(svc, "_resolve_client_by_type", lambda **kw: fake)
    res = await svc.test_connection_params(
        "mcp", {"server_url": "https://api.x.com/mcp", "auth_type": "oauth_app"}, {}
    )
    assert res["success"] is True
    assert res.get("requires_user_auth") is True


@pytest.mark.asyncio
async def test_oauth_mcp_test_unreachable_still_fails(monkeypatch):
    # A non-auth error (DNS/refused) is a real failure even in oauth mode.
    svc = ConnectionService()
    fake = _FakeClient(fail_message="Failed to connect to MCP server: [Errno -2] Name or service not known")
    monkeypatch.setattr(svc, "_resolve_client_by_type", lambda **kw: fake)
    res = await svc.test_connection_params(
        "mcp", {"server_url": "https://nope.invalid/mcp", "auth_type": "oauth_app"}, {}
    )
    assert res["success"] is False


@pytest.mark.asyncio
async def test_bearer_mcp_401_still_fails(monkeypatch):
    # bearer mode carries a token — a 401 is a genuine failure (bad token),
    # NOT "needs sign-in". Must not be reinterpreted as a pass.
    svc = ConnectionService()
    fake = _FakeClient(fail_message="Failed to connect to MCP server: Client error '401 Unauthorized'")
    monkeypatch.setattr(svc, "_resolve_client_by_type", lambda **kw: fake)
    res = await svc.test_connection_params(
        "mcp", {"server_url": "https://api.x.com/mcp", "auth_type": "bearer"}, {"token": "bad"}
    )
    assert res["success"] is False
