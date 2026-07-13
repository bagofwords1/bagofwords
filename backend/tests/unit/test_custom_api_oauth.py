"""Unit tests for per-user OAuth on the Custom API connector + the X Write preset.

Covers:
  - CustomApiClient sends the per-user OAuth access_token as Bearer (oauth_app).
  - Write endpoints (POST/PUT/PATCH/DELETE) are flagged for confirmation.
  - get_oauth_params resolves a custom_api oauth_app connection (Basic auth for X).
  - The registry exposes a custom_api `oauth_app` variant and the X Write preset.
"""
from unittest.mock import MagicMock

import pytest
import httpx

from app.data_sources.clients.custom_api_client import CustomApiClient
from app.services.connection_oauth_service import get_oauth_params
from app.schemas.data_source_registry import (
    custom_api_preset, custom_api_presets, get_entry,
)


def _conn(type="custom_api", credentials=None):
    c = MagicMock()
    c.id = "c1"
    c.type = type
    c.organization_id = "org-1"
    c.decrypt_credentials.return_value = credentials or {}
    return c


# --- CustomApiClient auth ----------------------------------------------------

class TestCustomApiClientAuth:
    def test_oauth_app_sends_access_token_as_bearer(self):
        client = CustomApiClient(
            base_url="https://api.x.com",
            auth_type="oauth_app",
            access_token="user_at_123",
        )
        headers = client._build_headers()
        assert headers["Authorization"] == "Bearer user_at_123"

    def test_oauth_type_sends_access_token_as_bearer(self):
        client = CustomApiClient(base_url="https://api.x.com", auth_type="oauth", access_token="t")
        assert client._build_headers()["Authorization"] == "Bearer t"

    def test_static_bearer_still_works(self):
        client = CustomApiClient(base_url="https://api.example.com", auth_type="bearer", token="static")
        assert client._build_headers()["Authorization"] == "Bearer static"

    def test_no_auth_sends_no_authorization(self):
        client = CustomApiClient(base_url="https://api.example.com", auth_type="none")
        assert "Authorization" not in client._build_headers()

    def test_oauth_without_token_sends_no_authorization(self):
        client = CustomApiClient(base_url="https://api.x.com", auth_type="oauth_app", access_token=None)
        assert "Authorization" not in client._build_headers()


# --- Write-endpoint confirmation --------------------------------------------

class TestWriteEndpointPolicy:
    def _client_with(self, endpoints):
        return CustomApiClient(base_url="https://api.x.com", auth_type="oauth_app", endpoints=endpoints)

    def test_post_endpoint_defaults_to_ask(self):
        c = self._client_with([{"name": "create_post", "method": "POST", "path": "/2/tweets"}])
        tools = c.list_tools()
        assert tools[0]["default_policy"] == "ask"

    def test_delete_endpoint_defaults_to_ask(self):
        c = self._client_with([{"name": "delete_post", "method": "DELETE", "path": "/2/tweets/{id}"}])
        assert c.list_tools()[0]["default_policy"] == "ask"

    def test_get_endpoint_has_no_default_policy(self):
        c = self._client_with([{"name": "get_thing", "method": "GET", "path": "/things"}])
        assert "default_policy" not in c.list_tools()[0]

    def test_confirm_true_overrides_read_method(self):
        c = self._client_with([{"name": "risky_get", "method": "GET", "path": "/x", "confirm": True}])
        assert c.list_tools()[0]["default_policy"] == "ask"

    def test_confirm_false_overrides_write_method(self):
        c = self._client_with([{"name": "safe_post", "method": "POST", "path": "/x", "confirm": False}])
        assert "default_policy" not in c.list_tools()[0]


# --- test_connection reachability (the OAuth-callback rollback bug) ----------

def _head_status(monkeypatch, status):
    """Patch httpx.Client so a HEAD returns the given status."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    transport = httpx.MockTransport(handler)
    original = httpx.Client

    class _Patched(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "Client", _Patched)


class TestConnectionReachability:
    def test_oauth_app_root_404_is_reachable(self, monkeypatch):
        # X's api.x.com root 404s; the callback must NOT treat that as a failure
        # (doing so rolled back a valid token and blocked sign-in).
        _head_status(monkeypatch, 404)
        client = CustomApiClient(base_url="https://api.x.com", auth_type="oauth_app")
        res = client.test_connection()
        assert res["success"] is True
        assert "sign-in required" in res["message"].lower()

    def test_oauth_app_401_is_reachable(self, monkeypatch):
        _head_status(monkeypatch, 401)
        client = CustomApiClient(base_url="https://api.x.com", auth_type="oauth_app")
        assert client.test_connection()["success"] is True

    def test_oauth_app_5xx_is_failure(self, monkeypatch):
        _head_status(monkeypatch, 503)
        client = CustomApiClient(base_url="https://api.x.com", auth_type="oauth_app")
        assert client.test_connection()["success"] is False

    def test_bearer_root_404_still_fails(self, monkeypatch):
        # Strict behavior preserved for data APIs — a 404 root is a likely misconfig.
        _head_status(monkeypatch, 404)
        client = CustomApiClient(base_url="https://api.example.com", auth_type="bearer", token="t")
        assert client.test_connection()["success"] is False

    def test_bearer_200_succeeds(self, monkeypatch):
        _head_status(monkeypatch, 200)
        client = CustomApiClient(base_url="https://api.example.com", auth_type="bearer", token="t")
        assert client.test_connection()["success"] is True


# --- Full Test-Connection path (test_connection_params) ----------------------

class TestConnectionParamsPath:
    """The pre-save 'Test Connection' button goes through
    ConnectionService.test_connection_params -> _resolve_client_by_type, which
    must preserve auth_type so an oauth_app API root 404 reads as reachable
    (not the misleading 'HTTP 404 — check the base URL' failure)."""

    def _cfg(self):
        return {
            "base_url": "https://api.x.com",
            "auth_type": "oauth_app",
            "headers": {},
            "endpoints": [
                {"name": "create_post", "method": "POST", "path": "/2/tweets",
                 "parameters": [{"name": "text", "in": "body", "type": "string", "required": True}]},
            ],
        }

    def test_resolve_client_preserves_auth_type(self):
        from app.services.connection_service import ConnectionService
        c = ConnectionService()._resolve_client_by_type("custom_api", self._cfg(), {"client_id": "x"})
        assert c.auth_type == "oauth_app"

    @pytest.mark.asyncio
    async def test_test_params_oauth_app_root_404_is_success(self, monkeypatch):
        import httpx
        from app.services.connection_service import ConnectionService

        def fake_head(self, url, **kw):
            return httpx.Response(404, request=httpx.Request("HEAD", url))

        monkeypatch.setattr(httpx.Client, "head", fake_head)
        res = await ConnectionService().test_connection_params(
            data_source_type="custom_api",
            config=self._cfg(),
            credentials={"client_id": "x", "client_secret": "y",
                         "token_url": "https://api.x.com/2/oauth2/token"},
        )
        # Reachable → lists the configured endpoints as tools.
        assert res["success"] is True
        assert "404" not in res["message"]


# --- get_oauth_params for custom_api ----------------------------------------

class TestCustomApiOAuthParams:
    def test_custom_api_oauth_app_resolves(self):
        conn = _conn(type="custom_api", credentials={
            "authorize_url": "https://x.com/i/oauth2/authorize",
            "token_url": "https://api.x.com/2/oauth2/token",
            "client_id": "cid",
            "client_secret": "csecret",
            "scopes": "tweet.read tweet.write offline.access",
            "token_endpoint_auth_method": "client_secret_basic",
        })
        params = get_oauth_params(conn)
        assert params["client_id"] == "cid"
        assert params["token_endpoint_auth_method"] == "client_secret_basic"
        assert params["token_url"] == "https://api.x.com/2/oauth2/token"

    def test_custom_api_x_host_infers_basic(self):
        conn = _conn(type="custom_api", credentials={
            "authorize_url": "https://x.com/i/oauth2/authorize",
            "token_url": "https://api.x.com/2/oauth2/token",
            "client_id": "cid",
            "client_secret": "csecret",
        })
        assert get_oauth_params(conn)["token_endpoint_auth_method"] == "client_secret_basic"


# --- Registry + preset -------------------------------------------------------

class TestCustomApiRegistryAndPreset:
    def test_custom_api_has_oauth_app_variant(self):
        entry = get_entry("custom_api")
        assert "oauth_app" in entry.credentials_auth.by_auth
        variant = entry.credentials_auth.by_auth["oauth_app"]
        assert "user" in variant.scopes

    def test_x_write_preset_shape(self):
        p = custom_api_preset("x_write")
        assert p is not None
        assert p.base_url == "https://api.x.com"
        assert p.auth == "oauth_app"
        names = [e["name"] for e in p.endpoints]
        assert "create_post" in names and "delete_post" in names

    def test_x_write_create_post_endpoint(self):
        p = custom_api_preset("x_write")
        cp = next(e for e in p.endpoints if e["name"] == "create_post")
        assert cp["method"] == "POST"
        assert cp["path"] == "/2/tweets"
        assert cp["confirm"] is True
        text_param = next(pr for pr in cp["parameters"] if pr["name"] == "text")
        assert text_param["in"] == "body" and text_param["required"] is True

    def test_x_write_oauth_defaults(self):
        p = custom_api_preset("x_write")
        d = p.oauth_defaults
        assert d.token_endpoint_auth_method == "client_secret_basic"
        assert "offline.access" in d.scopes
        assert "offline_access" not in d.scopes
        assert "tweet.write" in d.scopes

    def test_custom_api_presets_serialize(self):
        # Must round-trip through model_dump() for the catalog route.
        dumped = custom_api_presets()
        x = next(p for p in dumped if p["key"] == "x_write")
        assert x["oauth_defaults"]["token_endpoint_auth_method"] == "client_secret_basic"
        assert x["endpoints"][0]["name"] == "create_post"
