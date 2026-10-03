"""Unit tests for outbound MCP OAuth discovery + Dynamic Client Registration.

Uses httpx.MockTransport — no real MCP servers or authorization servers needed.

The fixture models the shape that matters: an MCP server whose authorization
server is a *generic identity provider* (Auth0 / Okta / Entra) fronting many
APIs. Such a provider's ``scopes_supported`` is the standard OIDC set — it says
nothing about what this MCP server needs. The server states its own
requirement in its 401 challenge and its protected-resource metadata, and
those are what a sign-in must request. Reading the provider's list instead
yields a token the MCP server rejects with 401 after a sign-in that looked
successful.

The server here belongs to no catalog preset: admins choose which third-party
services their org integrates, so discovery and registration must work
against whatever host the connection points at.
"""
import json

import httpx
import pytest

from app.services.mcp_dcr_service import (
    discover_mcp_oauth, register_client, ensure_mcp_oauth_config,
    parse_www_authenticate, select_scopes, split_scopes,
)
from app.services.connection_oauth_service import get_oauth_params


# A host that is deliberately in no preset and never will be.
RESOURCE = "https://mcp.some-vendor.example.com"
SERVER_URL = f"{RESOURCE}/mcp"
AS_ORIGIN = "https://auth.some-vendor.example.com"
PRM_ROOT = f"{RESOURCE}/.well-known/oauth-protected-resource"
REDIRECT = "https://bow.example.com/api/connections/oauth/callback"

# What a generic identity provider advertises. Note: no resource scope here.
OIDC_SCOPES = ["openid", "profile", "email", "offline_access", "name", "address"]


def _mock_httpx(monkeypatch, handler):
    """Route every httpx.AsyncClient through `handler`."""
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class _Patched(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _Patched)


def _server(
    seen=None,
    *,
    challenge=True,
    challenge_scope="mcp:read",
    prm_url=PRM_ROOT,
    prm_scopes=("mcp:read",),
    as_scopes=tuple(OIDC_SCOPES),
    registration_endpoint=True,
    open_server=False,
):
    """A spec-shaped third-party MCP server + its generic-IdP authorization server.

    - `challenge`: answer the unauthenticated MCP request with a 401 Bearer
      challenge naming `resource_metadata` and (when `challenge_scope`) `scope`.
    - `prm_url`: the ONLY place protected-resource metadata is served. Defaults
      to the origin root — the path-scoped variant 404s, which is common in
      the wild and is why discovery must fall back.
    - `open_server`: the MCP endpoint answers 200 (no auth) — discovery must
      still work without a challenge.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if seen is not None:
            seen.append((request.method, url))

        if url == SERVER_URL and request.method == "POST":
            if open_server:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
            if not challenge:
                return httpx.Response(401, json={"error": "unauthorized"})
            parts = [f'resource_metadata="{prm_url}"']
            if challenge_scope:
                parts.append(f'scope="{challenge_scope}"')
            return httpx.Response(
                401, json={"error": "invalid_token"},
                headers={"WWW-Authenticate": "Bearer " + ", ".join(parts)},
            )
        if url == prm_url:
            body = {"resource": SERVER_URL, "authorization_servers": [AS_ORIGIN]}
            if prm_scopes is not None:
                body["scopes_supported"] = list(prm_scopes)
            return httpx.Response(200, json=body)
        if url == f"{AS_ORIGIN}/.well-known/oauth-authorization-server":
            body = {
                "issuer": AS_ORIGIN,
                "authorization_endpoint": f"{AS_ORIGIN}/authorize",
                "token_endpoint": f"{AS_ORIGIN}/oauth/token",
                "scopes_supported": list(as_scopes),
            }
            if registration_endpoint:
                body["registration_endpoint"] = f"{AS_ORIGIN}/oidc/register"
            return httpx.Response(200, json=body)
        if url == f"{AS_ORIGIN}/oidc/register":
            return httpx.Response(201, json={
                "client_id": "dyn-client-9",
                "redirect_uris": [REDIRECT],
                "token_endpoint_auth_method": "none",
                "registration_client_uri": f"{AS_ORIGIN}/oidc/register/dyn-client-9",
            })
        return httpx.Response(404, json={"error": "not_found"})

    return handler


class FakeConnection:
    """Minimal stand-in for a persisted Connection row."""

    def __init__(self, server_url=SERVER_URL, creds=None):
        self.id = "conn-1"
        self.type = "mcp"
        self.config = {"server_url": server_url}
        self._creds = dict(creds or {})

    def decrypt_credentials(self):
        return dict(self._creds)

    def encrypt_credentials(self, creds):
        self._creds = dict(creds)


class FakeDB:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        pass


def _registered(**extra):
    """Credentials of a connection that already went through DCR."""
    return {
        "authorize_url": f"{AS_ORIGIN}/authorize",
        "token_url": f"{AS_ORIGIN}/oauth/token",
        "client_id": "dyn-client-9",
        "client_secret": None,
        "audience": SERVER_URL,
        "dcr_registered": True,
        **extra,
    }


# ── Scope selection (pure) ──────────────────────────────────────────────────

class TestSelectScopes:
    def test_challenge_is_authoritative(self):
        scopes, source = select_scopes("mcp:read mcp:write", ["mcp:read"], OIDC_SCOPES)
        assert scopes == "mcp:read mcp:write offline_access"
        assert source == "challenge"

    def test_resource_metadata_when_no_challenge(self):
        scopes, source = select_scopes(None, ["mcp:read"], OIDC_SCOPES)
        assert scopes == "mcp:read offline_access"
        assert source == "resource_metadata"

    def test_authorization_server_only_as_last_resort(self):
        scopes, source = select_scopes(None, None, ["openid", "profile"])
        assert scopes == "openid profile"
        assert source == "authorization_server"

    def test_offline_access_only_when_as_supports_it(self):
        scopes, _ = select_scopes("mcp:read", None, ["openid"])
        assert scopes == "mcp:read"

    def test_offline_access_not_duplicated(self):
        scopes, _ = select_scopes("mcp:read offline_access", None, OIDC_SCOPES)
        assert scopes == "mcp:read offline_access"

    def test_nothing_advertised(self):
        assert select_scopes(None, None, None) == ("", "none")

    def test_split_scopes_accepts_lists_and_delimited_strings(self):
        assert split_scopes(["a", "b", "a"]) == ["a", "b"]
        assert split_scopes("a, b  c,a") == ["a", "b", "c"]
        assert split_scopes("") == []


# ── WWW-Authenticate parsing ───────────────────────────────────────────────

class TestParseWwwAuthenticate:
    def test_quoted_and_bare_params(self):
        p = parse_www_authenticate([
            f'Bearer resource_metadata="{PRM_ROOT}", scope="mcp:read", realm=mcp'
        ])
        assert p == {"resource_metadata": PRM_ROOT, "scope": "mcp:read", "realm": "mcp"}

    def test_scheme_is_case_insensitive_and_error_params_are_kept(self):
        p = parse_www_authenticate([
            'bearer error="invalid_token", error_description="Authentication required"'
        ])
        assert p["error"] == "invalid_token"
        assert p["error_description"] == "Authentication required"

    def test_non_bearer_challenges_are_ignored(self):
        assert parse_www_authenticate(['Basic realm="x"']) == {}
        assert parse_www_authenticate([]) == {}
        assert parse_www_authenticate(None) == {}

    def test_first_bearer_header_wins_when_several(self):
        p = parse_www_authenticate(['Basic realm="x"', 'Bearer scope="a b"'])
        assert p == {"scope": "a b"}


# ── Discovery ──────────────────────────────────────────────────────────────

class TestDiscovery:
    @pytest.mark.asyncio
    async def test_scopes_come_from_the_server_not_the_identity_provider(self, monkeypatch):
        """The regression: with a generic IdP the AS lists only OIDC scopes.
        The sign-in must request what the MCP server demands, plus
        offline_access for a refresh token — and none of the OIDC noise."""
        _mock_httpx(monkeypatch, _server())
        meta = await discover_mcp_oauth(SERVER_URL)
        assert meta["scopes"] == "mcp:read offline_access"
        assert meta["scopes_source"] == "challenge"
        assert "openid" not in meta["scopes"] and "profile" not in meta["scopes"]
        # The raw inputs are reported too, for the UI / logs.
        assert meta["challenge_scopes"] == "mcp:read"
        assert meta["resource_scopes"] == "mcp:read"
        assert meta["authorization_server_scopes"] == " ".join(OIDC_SCOPES)

    @pytest.mark.asyncio
    async def test_endpoints_and_resource(self, monkeypatch):
        _mock_httpx(monkeypatch, _server())
        meta = await discover_mcp_oauth(SERVER_URL)
        assert meta["authorize_url"] == f"{AS_ORIGIN}/authorize"
        assert meta["token_url"] == f"{AS_ORIGIN}/oauth/token"
        assert meta["registration_endpoint"] == f"{AS_ORIGIN}/oidc/register"
        # The RFC 9728 `resource` is what the token gets audience-bound to, so
        # it must come from the metadata, not from the URL the admin typed.
        assert meta["resource"] == SERVER_URL

    @pytest.mark.asyncio
    async def test_resource_metadata_scopes_when_challenge_names_none(self, monkeypatch):
        # SDK-built servers challenge with resource_metadata but no scope.
        _mock_httpx(monkeypatch, _server(challenge_scope=None))
        meta = await discover_mcp_oauth(SERVER_URL)
        assert meta["scopes"] == "mcp:read offline_access"
        assert meta["scopes_source"] == "resource_metadata"

    @pytest.mark.asyncio
    async def test_challenge_resource_metadata_url_is_tried_first(self, monkeypatch):
        # Metadata served ONLY where the challenge says — neither the
        # path-scoped nor the root well-known URL exists.
        custom = f"{RESOURCE}/.well-known/oauth-protected-resource/some/where"
        seen = []
        _mock_httpx(monkeypatch, _server(seen, prm_url=custom))
        meta = await discover_mcp_oauth(SERVER_URL)
        assert meta["scopes"] == "mcp:read offline_access"
        gets = [u for m, u in seen if m == "GET"]
        assert gets[0] == custom

    @pytest.mark.asyncio
    async def test_falls_back_to_root_protected_resource_metadata(self, monkeypatch):
        # No challenge at all: path-scoped first (RFC 9728 §3.1), then root.
        seen = []
        _mock_httpx(monkeypatch, _server(seen, challenge=False))
        meta = await discover_mcp_oauth(SERVER_URL)
        assert meta["scopes_source"] == "resource_metadata"
        gets = [u for m, u in seen if m == "GET"]
        assert gets.index(f"{PRM_ROOT}/mcp") < gets.index(PRM_ROOT)

    @pytest.mark.asyncio
    async def test_open_server_without_challenge_still_discovers(self, monkeypatch):
        _mock_httpx(monkeypatch, _server(open_server=True))
        meta = await discover_mcp_oauth(SERVER_URL)
        assert meta["authorize_url"] == f"{AS_ORIGIN}/authorize"
        assert meta["scopes_source"] == "resource_metadata"

    @pytest.mark.asyncio
    async def test_identity_provider_scopes_only_when_server_advertises_none(self, monkeypatch):
        _mock_httpx(monkeypatch, _server(challenge_scope=None, prm_scopes=None))
        meta = await discover_mcp_oauth(SERVER_URL)
        assert meta["scopes_source"] == "authorization_server"
        assert meta["scopes"] == " ".join(OIDC_SCOPES)

    @pytest.mark.asyncio
    async def test_raises_when_metadata_is_undiscoverable(self, monkeypatch):
        _mock_httpx(monkeypatch, lambda r: httpx.Response(404, json={"error": "nope"}))
        with pytest.raises(ValueError) as exc:
            await discover_mcp_oauth(SERVER_URL)
        # The message reaches the UI as the 400's `detail`, so it has to say
        # what went wrong rather than just carry a status code.
        assert "Could not discover OAuth metadata" in str(exc.value)
        assert SERVER_URL in str(exc.value)


# ── Registration ───────────────────────────────────────────────────────────

class TestRegistration:
    @pytest.mark.asyncio
    async def test_registers_public_client_with_pkce(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == f"{AS_ORIGIN}/oidc/register":
                captured.update(json.loads(request.content.decode()))
                return httpx.Response(201, json={"client_id": "dyn-client-9"})
            return httpx.Response(404)

        _mock_httpx(monkeypatch, handler)
        reg = await register_client(f"{AS_ORIGIN}/oidc/register", REDIRECT)
        assert reg["client_id"] == "dyn-client-9"
        assert captured["redirect_uris"] == [REDIRECT]
        assert captured["token_endpoint_auth_method"] == "none"
        assert "authorization_code" in captured["grant_types"]
        assert "refresh_token" in captured["grant_types"]

    @pytest.mark.asyncio
    async def test_registration_failure_surfaces_provider_response(self, monkeypatch):
        _mock_httpx(monkeypatch, lambda r: httpx.Response(400, text='{"error":"invalid_redirect_uri"}'))
        with pytest.raises(ValueError) as exc:
            await register_client(f"{AS_ORIGIN}/oidc/register", "https://x/cb")
        assert "invalid_redirect_uri" in str(exc.value)


# ── ensure_mcp_oauth_config ────────────────────────────────────────────────

def _registrations(seen):
    return [u for m, u in seen if m == "POST" and u.endswith("/oidc/register")]


class TestEnsureMcpOauthConfig:
    @pytest.mark.asyncio
    async def test_registers_against_a_non_catalog_host(self, monkeypatch):
        """Any admin-configured MCP server must work. This used to raise
        "DCR is not allowed for host ..." for every server outside the preset
        catalog, surfacing as an unexplained 400 from /oauth/authorize."""
        _mock_httpx(monkeypatch, _server())
        conn = FakeConnection()

        assert await ensure_mcp_oauth_config(FakeDB(), conn) is True

        creds = conn.decrypt_credentials()
        assert creds["client_id"] == "dyn-client-9"
        assert creds["authorize_url"] == f"{AS_ORIGIN}/authorize"
        assert creds["token_url"] == f"{AS_ORIGIN}/oauth/token"
        assert creds["audience"] == SERVER_URL
        assert creds["dcr_registered"] is True
        # Discovery result lives under its own key; `scopes` stays the admin's.
        assert creds["discovered_scopes"] == "mcp:read offline_access"
        assert creds["scopes_source"] == "challenge"
        assert "scopes" not in creds

    @pytest.mark.asyncio
    async def test_second_call_reuses_the_client(self, monkeypatch):
        # A new client per sign-in would pile up dead registrations.
        seen = []
        _mock_httpx(monkeypatch, _server(seen))
        conn = FakeConnection()
        assert await ensure_mcp_oauth_config(FakeDB(), conn) is True
        assert await ensure_mcp_oauth_config(FakeDB(), conn) is False
        assert len(_registrations(seen)) == 1

    @pytest.mark.asyncio
    async def test_registered_connection_refreshes_scopes_from_the_server(self, monkeypatch):
        # The spec makes the live challenge authoritative, and a value baked in
        # at registration can't otherwise be corrected without re-creating the
        # connection.
        seen = []
        _mock_httpx(monkeypatch, _server(seen, challenge_scope="mcp:read mcp:write"))
        conn = FakeConnection(creds=_registered(discovered_scopes="mcp:read offline_access",
                                                scopes_source="challenge"))
        db = FakeDB()
        assert await ensure_mcp_oauth_config(db, conn) is False
        creds = conn.decrypt_credentials()
        assert creds["discovered_scopes"] == "mcp:read mcp:write offline_access"
        assert creds["client_id"] == "dyn-client-9"
        assert _registrations(seen) == []
        assert db.commits == 1

    @pytest.mark.asyncio
    async def test_unchanged_scopes_do_not_rewrite_credentials(self, monkeypatch):
        _mock_httpx(monkeypatch, _server())
        conn = FakeConnection(creds=_registered(discovered_scopes="mcp:read offline_access",
                                                scopes_source="challenge"))
        db = FakeDB()
        await ensure_mcp_oauth_config(db, conn)
        assert db.commits == 0

    @pytest.mark.asyncio
    async def test_legacy_scopes_written_by_discovery_are_superseded(self, monkeypatch):
        """A connection registered before discovery had its own key holds the
        identity provider's OIDC scopes under `scopes` — exactly the value that
        made the MCP server reject the token. It must be treated as a stale
        discovery result, not as an admin's override, and replaced."""
        _mock_httpx(monkeypatch, _server())
        stale = "openid profile offline_access name given_name family_name email"
        conn = FakeConnection(creds=_registered(scopes=stale))
        db = FakeDB()
        assert await ensure_mcp_oauth_config(db, conn) is False
        creds = conn.decrypt_credentials()
        assert "scopes" not in creds
        assert creds["discovered_scopes"] == "mcp:read offline_access"
        assert db.commits == 1
        # …and the sign-in now requests the right thing.
        assert get_oauth_params(conn)["scopes"] == "mcp:read offline_access"

    @pytest.mark.asyncio
    async def test_admin_override_survives_refresh(self, monkeypatch):
        _mock_httpx(monkeypatch, _server())
        conn = FakeConnection(creds=_registered(
            scopes="custom:one", discovered_scopes="stale", scopes_source="challenge"))
        await ensure_mcp_oauth_config(FakeDB(), conn)
        creds = conn.decrypt_credentials()
        assert creds["scopes"] == "custom:one"
        assert creds["discovered_scopes"] == "mcp:read offline_access"
        assert get_oauth_params(conn)["scopes"] == "custom:one"

    @pytest.mark.asyncio
    async def test_discovery_failure_on_refresh_keeps_what_we_have(self, monkeypatch):
        _mock_httpx(monkeypatch, lambda r: httpx.Response(500))
        before = _registered(discovered_scopes="mcp:read offline_access", scopes_source="challenge")
        conn = FakeConnection(creds=dict(before))
        db = FakeDB()
        assert await ensure_mcp_oauth_config(db, conn) is False  # no exception
        assert conn.decrypt_credentials() == before
        assert db.commits == 0

    @pytest.mark.asyncio
    async def test_admin_supplied_client_is_left_alone(self, monkeypatch):
        """An admin-registered OAuth app: no discovery traffic at all."""
        seen = []
        _mock_httpx(monkeypatch, _server(seen))
        conn = FakeConnection(creds={
            "client_id": "admin-registered",
            "authorize_url": f"{AS_ORIGIN}/authorize",
            "token_url": f"{AS_ORIGIN}/oauth/token",
            "scopes": "whatever the admin said",
        })
        assert await ensure_mcp_oauth_config(FakeDB(), conn) is False
        assert seen == []
        assert get_oauth_params(conn)["scopes"] == "whatever the admin said"

    @pytest.mark.asyncio
    async def test_explains_when_server_has_no_dcr_support(self, monkeypatch):
        _mock_httpx(monkeypatch, _server(registration_endpoint=False))
        with pytest.raises(ValueError) as exc:
            await ensure_mcp_oauth_config(FakeDB(), FakeConnection())
        msg = str(exc.value)
        assert "registration_endpoint" in msg
        assert "manually" in msg  # tells the admin what to do instead

    @pytest.mark.asyncio
    async def test_requires_a_server_url(self, monkeypatch):
        _mock_httpx(monkeypatch, _server())
        conn = FakeConnection()
        conn.config = {}
        with pytest.raises(ValueError) as exc:
            await ensure_mcp_oauth_config(FakeDB(), conn)
        assert "server_url" in str(exc.value)

    @pytest.mark.asyncio
    async def test_ignores_non_mcp_connections(self, monkeypatch):
        _mock_httpx(monkeypatch, _server())
        conn = FakeConnection()
        conn.type = "postgresql"
        assert await ensure_mcp_oauth_config(FakeDB(), conn) is False


# ── Authorize-time params ──────────────────────────────────────────────────

class TestOauthParams:
    def test_discovered_scopes_when_no_override(self):
        conn = FakeConnection(creds=_registered(discovered_scopes="mcp:read offline_access"))
        p = get_oauth_params(conn)
        assert p["scopes"] == "mcp:read offline_access"
        assert p["client_id"] == "dyn-client-9"
        assert p["audience"] == SERVER_URL
        assert p["token_endpoint_auth_method"] == "none"  # public client

    def test_empty_override_defers_to_discovered(self):
        # The form sends scopes="" to clear an override.
        conn = FakeConnection(creds=_registered(scopes="", discovered_scopes="mcp:read"))
        assert get_oauth_params(conn)["scopes"] == "mcp:read"
