"""Unit tests for the web_fetch tool.

The full agent loop is exercised by e2e tests. Here we cover:
 - input validation (URL scheme, hostname)
 - SSRF host guard (loopback, private, link-local, metadata)
 - the org-settings feature gate (enable_web_fetch)
 - HTML smart parsing (title, OG metadata, JSON-LD, headings, body text)
 - non-text content-type filtering
 - truncation
 - redirect-to-private-host blocking
without spinning up a real HTTP server or database. The curl_cffi
AsyncSession is patched so no network traffic leaves the test.
"""
from __future__ import annotations

import socket
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.tools.implementations import web_fetch as web_fetch_module
from app.ai.tools.implementations.web_fetch import (
    MAX_RESPONSE_BYTES,
    MAX_TEXT_CHARS,
    WebFetchTool,
    _is_safe_host,
    _parse_html,
)
from app.ai.tools.schemas.web_fetch import WebFetchInput


class _FakeFeature:
    def __init__(self, value: bool):
        self.value = value


class _FakeSettings:
    def __init__(self, enable_web_fetch: Optional[bool]):
        self._enable = enable_web_fetch

    def get_config(self, key: str):
        if key == "enable_web_fetch" and self._enable is not None:
            return _FakeFeature(self._enable)
        return None


def _runtime_ctx(enable_web_fetch: Optional[bool] = True) -> dict:
    return {
        "settings": _FakeSettings(enable_web_fetch),
        "report": SimpleNamespace(id="report-1"),
        "organization": SimpleNamespace(id="org-1"),
        "current_user": SimpleNamespace(id="user-1"),
    }


class _FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        headers: Optional[dict] = None,
        content: bytes = b"",
        url: str = "https://example.com/",
        encoding: Optional[str] = "utf-8",
    ):
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content
        self.url = url
        self.encoding = encoding


def _patched_session(*responses: _FakeResponse):
    """Return a patch context that yields a curl_cffi.AsyncSession whose
    .get() returns the queued responses in order."""
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    session.get = AsyncMock(side_effect=list(responses))
    factory = MagicMock(return_value=session)
    return patch.object(web_fetch_module.cf_requests, "AsyncSession", factory), session


async def _collect(tool, tool_input, ctx):
    events = []
    async for evt in tool.run_stream(tool_input, ctx):
        events.append(evt)
    return events


def _end_payload(events):
    end = [e for e in events if e.type == "tool.end"]
    assert end, "expected a tool.end event"
    return end[-1].payload


# --- input validation -------------------------------------------------------


def test_input_requires_url():
    with pytest.raises(Exception):
        WebFetchInput()


def test_input_accepts_url():
    inp = WebFetchInput(url="https://example.com/")
    assert inp.url == "https://example.com/"


# --- SSRF host guard --------------------------------------------------------


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",
        "10.0.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "0.0.0.0",
        "::1",
    ],
)
def test_is_safe_host_rejects_non_public_ips(addr):
    with patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", (addr, 0))]):
        assert _is_safe_host("example.test") is False


def test_is_safe_host_accepts_public_ip():
    with patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 0))]):
        assert _is_safe_host("example.com") is True


def test_is_safe_host_rejects_literal_localhost():
    assert _is_safe_host("localhost") is False
    assert _is_safe_host("api.localhost") is False


def test_is_safe_host_rejects_unresolvable_host():
    with patch("socket.getaddrinfo", side_effect=socket.gaierror):
        assert _is_safe_host("nope.invalid") is False


# --- HTML parser (pure) -----------------------------------------------------


def test_parse_html_extracts_title_and_meta():
    html = """
    <html><head>
      <title>Some Product · Store</title>
      <meta name="description" content="A nice product">
      <meta property="og:title" content="Some Product">
      <meta property="og:price:amount" content="45.9">
      <meta property="og:price:currency" content="ILS">
    </head><body><p>Body text</p></body></html>
    """
    out = _parse_html(html)
    assert out["title"] == "Some Product · Store"
    assert out["description"] == "A nice product"
    assert out["metadata"]["og:price:amount"] == "45.9"
    assert out["metadata"]["og:price:currency"] == "ILS"
    assert "Body text" in out["text"]


def test_parse_html_extracts_json_ld():
    html = """
    <html><head>
      <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"Product","name":"Mascara","offers":{"price":71,"priceCurrency":"ILS"}}
      </script>
    </head><body>page</body></html>
    """
    out = _parse_html(html)
    assert out["json_ld"]
    block = out["json_ld"][0]
    assert block["@type"] == "Product"
    assert block["offers"]["price"] == 71


def test_parse_html_strips_scripts_and_styles_from_text():
    html = """
    <html><body>
      <script>var secret="should not appear";</script>
      <style>.x { color: red }</style>
      <noscript>js disabled</noscript>
      <nav>top nav links</nav>
      <main><p>Real article body.</p></main>
      <footer>footer junk</footer>
    </body></html>
    """
    out = _parse_html(html)
    assert "Real article body." in out["text"]
    assert "should not appear" not in out["text"]
    assert "color: red" not in out["text"]
    assert "footer junk" not in out["text"]


def test_parse_html_collects_headings():
    html = "<html><body><h1>Headline One</h1><p>x</p><h2>Sub two</h2></body></html>"
    out = _parse_html(html)
    assert out["headings"][:2] == ["Headline One", "Sub two"]


def test_parse_html_tolerates_invalid_json_ld():
    html = '<html><head><script type="application/ld+json">{not json</script></head><body>x</body></html>'
    out = _parse_html(html)
    assert out["json_ld"] == []


# --- feature gate -----------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_org_setting_blocks_fetch():
    tool = WebFetchTool()
    ctx = _runtime_ctx(enable_web_fetch=False)

    with patch.object(web_fetch_module.cf_requests, "AsyncSession") as mock_session:
        events = await _collect(tool, {"url": "https://example.com/"}, ctx)
        mock_session.assert_not_called()

    payload = _end_payload(events)
    assert payload["output"]["success"] is False
    assert "disabled" in payload["output"]["error_message"].lower()


@pytest.mark.asyncio
async def test_missing_settings_blocks_fetch():
    tool = WebFetchTool()
    ctx = {"report": SimpleNamespace(id="r")}

    with patch.object(web_fetch_module.cf_requests, "AsyncSession") as mock_session:
        events = await _collect(tool, {"url": "https://example.com/"}, ctx)
        mock_session.assert_not_called()

    payload = _end_payload(events)
    assert payload["output"]["success"] is False


@pytest.mark.asyncio
async def test_missing_feature_entry_blocks_fetch():
    tool = WebFetchTool()
    ctx = _runtime_ctx(enable_web_fetch=None)

    with patch.object(web_fetch_module.cf_requests, "AsyncSession") as mock_session:
        events = await _collect(tool, {"url": "https://example.com/"}, ctx)
        mock_session.assert_not_called()

    payload = _end_payload(events)
    assert payload["output"]["success"] is False


# --- URL validation ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_url",
    ["ftp://example.com/", "file:///etc/passwd", "javascript:alert(1)", "http://"],
)
async def test_rejects_non_http_schemes(bad_url):
    tool = WebFetchTool()
    ctx = _runtime_ctx()

    with patch.object(web_fetch_module.cf_requests, "AsyncSession") as mock_session:
        events = await _collect(tool, {"url": bad_url}, ctx)
        mock_session.assert_not_called()

    payload = _end_payload(events)
    assert payload["output"]["success"] is False


@pytest.mark.asyncio
async def test_rejects_non_public_host():
    tool = WebFetchTool()
    ctx = _runtime_ctx()

    with patch.object(web_fetch_module.cf_requests, "AsyncSession") as mock_session:
        events = await _collect(tool, {"url": "http://localhost/foo"}, ctx)
        mock_session.assert_not_called()

    payload = _end_payload(events)
    assert payload["output"]["success"] is False
    assert "non-public" in payload["output"]["error_message"].lower()


# --- happy paths ------------------------------------------------------------


@pytest.mark.asyncio
async def test_html_response_returns_structured_fields():
    body = (
        b"<html><head><title>Hello</title>"
        b'<meta property="og:title" content="Hello OG">'
        b'<meta name="description" content="Greeting page">'
        b'<script type="application/ld+json">{"@type":"WebPage","name":"Hello"}</script>'
        b"</head><body><h1>Hi there</h1><p>Body content here.</p>"
        b"<script>var x=1;</script></body></html>"
    )
    fake = _FakeResponse(
        status_code=200,
        headers={"content-type": "text/html; charset=utf-8"},
        content=body,
        url="https://example.com/p",
    )
    ctx_p, session = _patched_session(fake)
    tool = WebFetchTool()
    with patch.object(web_fetch_module, "_is_safe_host", return_value=True), ctx_p:
        events = await _collect(tool, {"url": "https://example.com/p"}, _runtime_ctx())

    out = _end_payload(events)["output"]
    assert out["success"] is True
    assert out["status_code"] == 200
    assert out["title"] == "Hello"
    assert out["description"] == "Greeting page"
    assert out["metadata"]["og:title"] == "Hello OG"
    assert out["json_ld"][0]["@type"] == "WebPage"
    assert "Hi there" in out["headings"]
    assert "Body content here." in out["content"]
    assert "var x=1" not in out["content"]


@pytest.mark.asyncio
async def test_json_api_response_returned_raw():
    body = b'{"price": 71, "currency": "ILS"}'
    fake = _FakeResponse(
        status_code=200,
        headers={"content-type": "application/json"},
        content=body,
        url="https://api.example.com/item",
    )
    ctx_p, _ = _patched_session(fake)
    tool = WebFetchTool()
    with patch.object(web_fetch_module, "_is_safe_host", return_value=True), ctx_p:
        events = await _collect(tool, {"url": "https://api.example.com/item"}, _runtime_ctx())

    out = _end_payload(events)["output"]
    assert out["success"] is True
    assert out["content"] == '{"price": 71, "currency": "ILS"}'
    # Structured fields stay empty for non-HTML
    assert out["title"] is None
    assert out["json_ld"] is None


@pytest.mark.asyncio
async def test_truncates_large_text_response():
    big = b"x" * (MAX_RESPONSE_BYTES + 5000)
    fake = _FakeResponse(
        status_code=200,
        headers={"content-type": "text/plain"},
        content=big,
        url="https://example.com/big",
    )
    ctx_p, _ = _patched_session(fake)
    tool = WebFetchTool()
    with patch.object(web_fetch_module, "_is_safe_host", return_value=True), ctx_p:
        events = await _collect(tool, {"url": "https://example.com/big"}, _runtime_ctx())

    out = _end_payload(events)["output"]
    assert out["success"] is True
    assert out["truncated"] is True
    assert len(out["content"]) <= MAX_TEXT_CHARS


@pytest.mark.asyncio
async def test_skips_non_text_content_type():
    fake = _FakeResponse(
        status_code=200,
        headers={"content-type": "image/png"},
        content=b"\x89PNG\r\n",
        url="https://example.com/img",
    )
    ctx_p, _ = _patched_session(fake)
    tool = WebFetchTool()
    with patch.object(web_fetch_module, "_is_safe_host", return_value=True), ctx_p:
        events = await _collect(tool, {"url": "https://example.com/img"}, _runtime_ctx())

    out = _end_payload(events)["output"]
    assert out["success"] is True
    assert out["content"] is None
    assert "image/png" in out["error_message"]


@pytest.mark.asyncio
async def test_blocks_redirect_to_private_host():
    redirect = _FakeResponse(
        status_code=302,
        headers={"location": "http://10.0.0.1/internal"},
        content=b"",
        url="https://example.com/r",
    )
    ctx_p, _ = _patched_session(redirect)
    tool = WebFetchTool()
    counter = {"n": 0}

    def fake_safe(host):
        counter["n"] += 1
        return counter["n"] == 1  # first call (initial) safe; redirect target unsafe

    with patch.object(web_fetch_module, "_is_safe_host", side_effect=fake_safe), ctx_p:
        events = await _collect(tool, {"url": "https://example.com/r"}, _runtime_ctx())

    out = _end_payload(events)["output"]
    assert out["success"] is False
    assert "redirect" in out["error_message"].lower()


@pytest.mark.asyncio
async def test_follows_redirect_to_safe_host():
    redirect = _FakeResponse(
        status_code=302,
        headers={"location": "/landing"},
        content=b"",
        url="https://example.com/start",
    )
    final = _FakeResponse(
        status_code=200,
        headers={"content-type": "text/html"},
        content=b"<html><head><title>Landed</title></head><body>ok</body></html>",
        url="https://example.com/landing",
    )
    ctx_p, _ = _patched_session(redirect, final)
    tool = WebFetchTool()
    with patch.object(web_fetch_module, "_is_safe_host", return_value=True), ctx_p:
        events = await _collect(tool, {"url": "https://example.com/start"}, _runtime_ctx())

    out = _end_payload(events)["output"]
    assert out["success"] is True
    assert out["title"] == "Landed"
