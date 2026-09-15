"""Unit tests for outbound MCP OAuth discovery + Dynamic Client Registration.

Uses httpx.MockTransport — no real MCP servers or authorization servers needed.

The case these lock down: an MCP server that belongs to no catalog preset.
Admins choose which third-party services their org integrates, so discovery and
registration must run against whatever host the connection points at. An
allowlist of catalog hosts here means "Verify" passes while "Sign in" dies with
a bare 400, which is indistinguishable from a broken provider.
"""
import httpx
import pytest

from app.services.mcp_dcr_service import (
    discover_mcp_oauth, register_client, ensure_mcp_oauth_config,
)


# A host that is deliberately in no preset and never will be.
RESOURCE = "https://mcp.some-vendor.example.com"
SERVER_URL = f"{RESOURCE}/mcp"
AS_ORIGIN = "https://auth.some-vendor.example.com"


def _mock_httpx(monkeypatch, handler):
    """Route every httpx.AsyncClient through `handler`."""
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class _Patched(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _Patched)


def _default_handler(seen=None, *, registration_endpoint=True):
    """A spec-shaped third-party server.

    Protected-resource metadata is served ONLY at the origin root: the
    path-scoped variant 404s, which is common in the wild and is why the
    root-level fallback in discovery has to exist.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if seen is not None:
            seen.append((request.method, url))

        if url == f"{RESOURCE}/.well-known/oauth-protected-resource":
            return httpx.Response(200, json={
                "resource": SERVER_URL,
                "authorization_servers": [AS_ORIGIN],
                "scopes_supported": ["mcp:read"],
            })
        if url == f"{AS_ORIGIN}/.well-known/oauth-authorization-server":
            body = {
                "issuer": AS_ORIGIN,
                "authorization_endpoint": f"{AS_ORIGIN}/authorize",
                "token_endpoint": f"{AS_ORIGIN}/oauth/token",
                "scopes_supported": ["mcp:read", "offline_access"],
            }
            if registration_endpoint:
                body["registration_endpoint"] = f"{AS_ORIGIN}/oidc/register"
            return httpx.Response(200, json=body)
        if url == f"{AS_ORIGIN}/oidc/register":
            return httpx.Response(201, json={
                "client_id": "dyn-client-9",
                "redirect_uris": ["https://bow.example.com/api/connections/oauth/callback"],
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
    async def commit(self):
        pass

    async def refresh(self, obj):
        pass


class TestDiscovery:
    @pytest.mark.asyncio
    async def test_discovers_non_catalog_server(self, monkeypatch):
        _mock_httpx(monkeypatch, _default_handler())
        meta = await discover_mcp_oauth(SERVER_URL)
        assert meta["authorize_url"] == f"{AS_ORIGIN}/authorize"
        assert meta["token_url"] == f"{AS_ORIGIN}/oauth/token"
        assert meta["registration_endpoint"] == f"{AS_ORIGIN}/oidc/register"
        # The RFC 9728 `resource` is what the token gets audience-bound to, so
        # it must come from the metadata, not from the URL the admin typed.
        assert meta["resource"] == SERVER_URL
        assert meta["scopes_supported"] == "mcp:read offline_access"

    @pytest.mark.asyncio
    async def test_falls_back_to_root_protected_resource_metadata(self, monkeypatch):
        seen = []
        _mock_httpx(monkeypatch, _default_handler(seen))
        await discover_mcp_oauth(SERVER_URL)
        probed = [u for _, u in seen]
        # Path-scoped first (RFC 9728 §3.1), then the origin root.
        assert f"{RESOURCE}/.well-known/oauth-protected-resource/mcp" in probed
        assert f"{RESOURCE}/.well-known/oauth-protected-resource" in probed
        assert probed.index(f"{RESOURCE}/.well-known/oauth-protected-resource/mcp") < \
            probed.index(f"{RESOURCE}/.well-known/oauth-protected-resource")

    @pytest.mark.asyncio
    async def test_raises_when_metadata_is_undiscoverable(self, monkeypatch):
        _mock_httpx(monkeypatch, lambda r: httpx.Response(404, json={"error": "nope"}))
        with pytest.raises(ValueError) as exc:
            await discover_mcp_oauth(SERVER_URL)
        # The message reaches the UI as the 400's `detail`, so it has to say
        # what went wrong rather than just carry a status code.
        assert "Could not discover OAuth metadata" in str(exc.value)
        assert SERVER_URL in str(exc.value)


class TestRegistration:
    @pytest.mark.asyncio
    async def test_registers_public_client_with_pkce(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == f"{AS_ORIGIN}/oidc/register":
                import json as _json
                captured.update(_json.loads(request.content.decode()))
                return httpx.Response(201, json={"client_id": "dyn-client-9"})
            return httpx.Response(404)

        _mock_httpx(monkeypatch, handler)
        reg = await register_client(
            f"{AS_ORIGIN}/oidc/register",
            "https://bow.example.com/api/connections/oauth/callback",
        )
        assert reg["client_id"] == "dyn-client-9"
        assert captured["redirect_uris"] == [
            "https://bow.example.com/api/connections/oauth/callback"]
        assert captured["token_endpoint_auth_method"] == "none"
        assert "authorization_code" in captured["grant_types"]
        assert "refresh_token" in captured["grant_types"]

    @pytest.mark.asyncio
    async def test_registration_failure_surfaces_provider_response(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text='{"error":"invalid_redirect_uri"}')

        _mock_httpx(monkeypatch, handler)
        with pytest.raises(ValueError) as exc:
            await register_client(f"{AS_ORIGIN}/oidc/register", "https://x/cb")
        assert "invalid_redirect_uri" in str(exc.value)


class TestEnsureMcpOauthConfig:
    @pytest.mark.asyncio
    async def test_registers_against_a_non_catalog_host(self, monkeypatch):
        """The regression guard: any admin-configured MCP server must work.

        This used to raise "DCR is not allowed for host ..." for every server
        outside the preset catalog, which surfaced as an unexplained 400 from
        /connections/{id}/oauth/authorize.
        """
        _mock_httpx(monkeypatch, _default_handler())
        conn = FakeConnection()

        assert await ensure_mcp_oauth_config(FakeDB(), conn) is True

        creds = conn.decrypt_credentials()
        assert creds["client_id"] == "dyn-client-9"
        assert creds["authorize_url"] == f"{AS_ORIGIN}/authorize"
        assert creds["token_url"] == f"{AS_ORIGIN}/oauth/token"
        assert creds["audience"] == SERVER_URL
        assert creds["dcr_registered"] is True

    @pytest.mark.asyncio
    async def test_is_idempotent(self, monkeypatch):
        _mock_httpx(monkeypatch, _default_handler())
        conn = FakeConnection()
        assert await ensure_mcp_oauth_config(FakeDB(), conn) is True
        # Second call must not re-register: a new client per sign-in would pile
        # up dead registrations on the provider.
        assert await ensure_mcp_oauth_config(FakeDB(), conn) is False

    @pytest.mark.asyncio
    async def test_noop_when_client_preconfigured(self, monkeypatch):
        """An admin-supplied OAuth client wins; no discovery traffic at all."""
        seen = []
        _mock_httpx(monkeypatch, _default_handler(seen))
        conn = FakeConnection(creds={
            "client_id": "admin-registered",
            "authorize_url": f"{AS_ORIGIN}/authorize",
            "token_url": f"{AS_ORIGIN}/oauth/token",
        })
        assert await ensure_mcp_oauth_config(FakeDB(), conn) is False
        assert seen == []

    @pytest.mark.asyncio
    async def test_explains_when_server_has_no_dcr_support(self, monkeypatch):
        _mock_httpx(monkeypatch, _default_handler(registration_endpoint=False))
        with pytest.raises(ValueError) as exc:
            await ensure_mcp_oauth_config(FakeDB(), FakeConnection())
        msg = str(exc.value)
        assert "registration_endpoint" in msg
        # Tells the admin what to do instead of just failing.
        assert "manually" in msg

    @pytest.mark.asyncio
    async def test_requires_a_server_url(self, monkeypatch):
        _mock_httpx(monkeypatch, _default_handler())
        conn = FakeConnection()
        conn.config = {}
        with pytest.raises(ValueError) as exc:
            await ensure_mcp_oauth_config(FakeDB(), conn)
        assert "server_url" in str(exc.value)

    @pytest.mark.asyncio
    async def test_ignores_non_mcp_connections(self, monkeypatch):
        _mock_httpx(monkeypatch, _default_handler())
        conn = FakeConnection()
        conn.type = "postgresql"
        assert await ensure_mcp_oauth_config(FakeDB(), conn) is False
