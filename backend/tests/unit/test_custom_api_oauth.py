"""Unit tests for per-user OAuth on the Custom API connector + the X Write preset.

Covers:
  - CustomApiClient sends the per-user OAuth access_token as Bearer (oauth_app).
  - Write endpoints (POST/PUT/PATCH/DELETE) are flagged for confirmation.
  - get_oauth_params resolves a custom_api oauth_app connection (Basic auth for X).
  - The registry exposes a custom_api `oauth_app` variant and the X Write preset.
"""
from unittest.mock import MagicMock

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
