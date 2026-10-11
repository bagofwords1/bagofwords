"""Scheme checks for browser navigation targets (OAuth redirect/authorize URLs)."""

import pytest

from app.core.url_safety import is_safe_http_url, is_safe_redirect_uri

DANGEROUS = [
    "javascript:alert(1)", "JavaScript:alert(1)", " javascript:alert(1)", "javascript:alert(1) ",
    "java\tscript:alert(1)", "java\rscript:alert(1)", "​javascript:alert(1)", "\x7fjavascript:x",
    "data:text/html,x", "DATA:text/html,x", "vbscript:x", "file:///etc/passwd", "blob:https://a/b",
    "about:blank", "filesystem:https://a/b", "view-source:https://a", "", "/cb", "//evil.com/cb",
    "cb", "http://", "https:///path", "https:evil.com", "1http://a", None, 42, ["https://a"],
]


@pytest.mark.parametrize("url", DANGEROUS)
def test_dangerous_or_malformed_rejected_everywhere(url):
    assert not is_safe_redirect_uri(url)
    assert not is_safe_http_url(url)


@pytest.mark.parametrize("url", [
    "https://claude.ai/api/mcp/auth_callback", "HTTPS://Example.com/cb?x=1",
    "http://localhost:6274/oauth/callback", "http://127.0.0.1:5000/cb", "http://[::1]:8080/cb",
    "http://intranet.corp/cb",
])
def test_http_urls_allowed_for_both(url):
    assert is_safe_redirect_uri(url)
    assert is_safe_http_url(url)


@pytest.mark.parametrize("url", [
    "cursor://anysphere.cursor-retrieval/oauth/callback", "vscode://vscode.github-authentication/cb",
    "claude://oauth/callback", "com.example.app:/oauth2redirect",
])
def test_app_schemes_allowed_as_redirect_but_not_as_browser_authorize_url(url):
    assert is_safe_redirect_uri(url)
    assert not is_safe_http_url(url)


# ── MCP discovery: a malicious server can't hand us a javascript: authorize URL

@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["javascript:alert(1)//", " JAVASCRIPT:alert(1)", "data:text/html,x"])
async def test_mcp_discovery_rejects_non_http_authorization_endpoint(monkeypatch, endpoint):
    import httpx

    from app.services.mcp_dcr_service import discover_mcp_oauth

    server, auth = "https://mcp.vendor.example.com/mcp", "https://auth.vendor.example.com"
    prm = "https://mcp.vendor.example.com/.well-known/oauth-protected-resource"

    def handler(request):
        url = str(request.url)
        if url == server:
            return httpx.Response(401, headers={"WWW-Authenticate": f'Bearer resource_metadata="{prm}"'})
        if url == prm:
            return httpx.Response(200, json={"resource": server, "authorization_servers": [auth]})
        if url == f"{auth}/.well-known/oauth-authorization-server":
            return httpx.Response(200, json={"issuer": auth, "authorization_endpoint": endpoint,
                                             "token_endpoint": f"{auth}/token"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class _Patched(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _Patched)
    with pytest.raises(ValueError):
        await discover_mcp_oauth(server)
