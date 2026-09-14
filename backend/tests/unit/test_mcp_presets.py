"""Unit tests for the MCP connector presets and the data_shape license gate."""
import pytest

from app.schemas.data_source_registry import (
    mcp_presets, mcp_preset,
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
    # GitHub needs an OAuth app.
    assert mcp_preset("github").auth == "oauth_app"


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


# ── Preset-scoped form defaults ────────────────────────────────────────────
# oauth_app presets carry their provider OAuth constants so the connect form can
# pre-fill them (the admin only supplies client_id/secret) instead of asking for
# invariant endpoints by hand.

def test_oauth_app_presets_prefill_endpoints():
    for key in ("x", "github", "google_drive", "hubspot"):
        d = mcp_preset(key).oauth_defaults
        assert d is not None, f"{key} should carry oauth_defaults"
        assert d.authorize_url and d.authorize_url.startswith("https://")
        assert d.token_url and d.token_url.startswith("https://")


def test_x_oauth_defaults_are_correct():
    d = mcp_preset("x").oauth_defaults
    assert d.authorize_url == "https://x.com/i/oauth2/authorize"
    assert d.token_url == "https://api.x.com/2/oauth2/token"
    # tweet.write (not the invalid twitter.write) is the X scope for posting.
    assert "tweet.write" in d.scopes
    assert "twitter.write" not in d.scopes
    # X spells the refresh-token scope offline.access (dot), never
    # offline_access (underscore) — the underscore makes X drop the refresh
    # token. Guard both directions.
    assert "offline.access" in d.scopes
    assert "offline_access" not in d.scopes
    # X is a confidential client: token exchange must use HTTP Basic auth.
    assert d.token_endpoint_auth_method == "client_secret_basic"


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
    assert mcp_preset("hubspot").allowed_auth == ["oauth_app"]


# ── HubSpot ────────────────────────────────────────────────────────────────
# HubSpot's hosted CRM MCP server. Probed live 2026-09; each assertion below
# pins something the probe established and that is easy to "correct" wrongly.

def test_hubspot_preset_is_oauth_app_not_dcr():
    # HubSpot's AS metadata advertises no registration_endpoint, so the tile must
    # not offer the zero-setup DCR path — it needs a registered HubSpot app.
    hs = mcp_preset("hubspot")
    assert hs is not None, "hubspot must be in the MCP catalog"
    assert hs.auth == "oauth_app"
    assert "dcr" not in (hs.allowed_auth or [])


def test_hubspot_server_url_has_no_path():
    # HubSpot serves MCP at the ROOT of mcp.hubspot.com; /mcp is a 404. The
    # connect form matches presets by exact server_url, so a path here breaks
    # both the connection and preset recognition in edit mode.
    assert mcp_preset("hubspot").server_url == "https://mcp.hubspot.com"


def test_hubspot_uses_mcp_scoped_oauth_endpoints():
    # The MCP server advertises its own endpoints, NOT HubSpot's classic
    # app.hubspot.com / api.hubapi.com OAuth pair.
    d = mcp_preset("hubspot").oauth_defaults
    assert d.authorize_url == "https://mcp.hubspot.com/oauth/authorize/user"
    assert d.token_url == "https://mcp.hubspot.com/oauth/v3/token"
    assert "app.hubspot.com" not in d.authorize_url
    assert "api.hubapi.com" not in d.token_url


def test_hubspot_token_auth_and_audience_defaults():
    d = mcp_preset("hubspot").oauth_defaults
    # HubSpot advertises client_secret_post, which is our default → left unset
    # rather than restated (X sets client_secret_basic because it differs).
    assert d.token_endpoint_auth_method is None
    # HubSpot does not advertise RFC 8707 resource indicators; sending an
    # unexpected `resource` on the token request risks a rejection.
    assert d.audience is None


def test_hubspot_scopes_are_read_only_and_normalize():
    from app.routes.connection_oauth import _normalize_scopes
    d = mcp_preset("hubspot").oauth_defaults
    normalized = _normalize_scopes(d.scopes)
    scopes = normalized.split()
    # `oauth` is required of every HubSpot app; the CRM scopes are the read-only
    # set available on every portal tier.
    assert "oauth" in scopes
    assert "crm.objects.contacts.read" in scopes
    # Default must not request write access.
    assert not [s for s in scopes if s.endswith(".write")]
    # RFC 6749 wants space-delimited scopes on the authorize request.
    assert "," not in normalized


def test_hubspot_sample_tools_are_the_discovered_ones():
    # Filled from a live tools/list against a real portal (2026-09), not guessed.
    # query_crm_data is the one that matters: HubSpot CRM over SQL.
    tools = mcp_preset("hubspot").sample_tools
    assert tools and "query_crm_data" in tools
    assert {"search_crm_objects", "get_properties", "search_properties"} <= set(tools)


def test_hubspot_preset_scopes_are_documentation_only():
    # A HubSpot MCP Connector app grants a fixed bundle from its own config; the
    # `scope` parameter does not control it (verified live: 4 requested, 37
    # granted). The value is kept as guidance for configuring the app, so it must
    # stay read-only and must not imply write access.
    from app.routes.connection_oauth import _normalize_scopes
    scopes = _normalize_scopes(mcp_preset("hubspot").oauth_defaults.scopes).split()
    assert not [s for s in scopes if s.endswith(".write")]


def test_hubspot_is_a_services_tile_and_serializes():
    hs = next(p for p in mcp_presets() if p["key"] == "hubspot")
    assert hs["category"] == "services"          # a SaaS app, like Salesforce
    assert hs["transport"] == "streamable_http"
    assert hs["title"] == "HubSpot"
    assert hs["oauth_defaults"]["authorize_url"].startswith("https://mcp.hubspot.com/")


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
    assert x["oauth_defaults"]["authorize_url"] == "https://x.com/i/oauth2/authorize"
    # token_endpoint_auth_method must serialize through so the connect form can
    # prefill it (X → client_secret_basic).
    assert x["oauth_defaults"]["token_endpoint_auth_method"] == "client_secret_basic"


def test_sample_tools_present():
    # X keeps a hand-curated sample (not in the Anthropic directory).
    assert "get_users_by_username" in (mcp_preset("x").sample_tools or [])
    # Directory-sourced previews for the DCR / Google presets.
    assert "search" in (mcp_preset("notion").sample_tools or [])
    assert mcp_preset("monday").sample_tools  # populated from the directory
    assert mcp_preset("linear").sample_tools
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
