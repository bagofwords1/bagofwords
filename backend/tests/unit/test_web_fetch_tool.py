"""Unit tests for the web_fetch tool.

The full agent loop is exercised by e2e tests. Here we cover:
 - input validation (URL scheme, hostname)
 - SSRF host guard (loopback, private, link-local, metadata)
 - the org-settings feature gate (enable_web_fetch)
 - response truncation
 - content-type filtering
 - redirect-to-private-host blocking
without spinning up a real HTTP server or database.
"""
from __future__ import annotations

import socket
from types import SimpleNamespace
from typing import Callable, Optional
from unittest.mock import patch

import httpx
import pytest

from app.ai.tools.implementations import web_fetch as web_fetch_module
from app.ai.tools.implementations.web_fetch import (
    MAX_RESPONSE_BYTES,
    MAX_TEXT_CHARS,
    WebFetchTool,
    _is_safe_host,
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


async def _collect(tool, tool_input, ctx):
    events = []
    async for evt in tool.run_stream(tool_input, ctx):
        events.append(evt)
    return events


def _end_payload(events):
    end = [e for e in events if e.type == "tool.end"]
    assert end, "expected a tool.end event"
    return end[-1].payload


def _patched_client(handler: Callable[[httpx.Request], httpx.Response]):
    """Return a patch context that injects an httpx.MockTransport into the tool."""
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    return patch.object(web_fetch_module.httpx, "AsyncClient", side_effect=factory)


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


def test_is_safe_host_rejects_empty():
    assert _is_safe_host("") is False


# --- feature gate -----------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_org_setting_blocks_fetch():
    tool = WebFetchTool()
    ctx = _runtime_ctx(enable_web_fetch=False)

    with patch.object(web_fetch_module.httpx, "AsyncClient") as mock_client:
        events = await _collect(tool, {"url": "https://example.com/"}, ctx)
        mock_client.assert_not_called()

    payload = _end_payload(events)
    assert payload["output"]["success"] is False
    assert "disabled" in payload["output"]["error_message"].lower()


@pytest.mark.asyncio
async def test_missing_settings_blocks_fetch():
    tool = WebFetchTool()
    ctx = {"report": SimpleNamespace(id="report-1")}

    with patch.object(web_fetch_module.httpx, "AsyncClient") as mock_client:
        events = await _collect(tool, {"url": "https://example.com/"}, ctx)
        mock_client.assert_not_called()

    payload = _end_payload(events)
    assert payload["output"]["success"] is False


@pytest.mark.asyncio
async def test_missing_feature_entry_blocks_fetch():
    """If get_config returns None (feature not present), fetch must not run."""
    tool = WebFetchTool()
    ctx = _runtime_ctx(enable_web_fetch=None)

    with patch.object(web_fetch_module.httpx, "AsyncClient") as mock_client:
        events = await _collect(tool, {"url": "https://example.com/"}, ctx)
        mock_client.assert_not_called()

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

    with patch.object(web_fetch_module.httpx, "AsyncClient") as mock_client:
        events = await _collect(tool, {"url": bad_url}, ctx)
        mock_client.assert_not_called()

    payload = _end_payload(events)
    assert payload["output"]["success"] is False


@pytest.mark.asyncio
async def test_rejects_non_public_host():
    tool = WebFetchTool()
    ctx = _runtime_ctx()

    with patch.object(web_fetch_module.httpx, "AsyncClient") as mock_client:
        events = await _collect(tool, {"url": "http://localhost/foo"}, ctx)
        mock_client.assert_not_called()

    payload = _end_payload(events)
    assert payload["output"]["success"] is False
    assert "non-public" in payload["output"]["error_message"].lower()


# --- happy path & response handling -----------------------------------------


@pytest.mark.asyncio
async def test_fetches_text_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="hello world",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    tool = WebFetchTool()
    ctx = _runtime_ctx()
    with patch.object(web_fetch_module, "_is_safe_host", return_value=True), _patched_client(handler):
        events = await _collect(tool, {"url": "https://example.com/page"}, ctx)

    out = _end_payload(events)["output"]
    assert out["success"] is True
    assert out["status_code"] == 200
    assert out["content"] == "hello world"
    assert out["truncated"] is False
    assert out["content_type"].startswith("text/html")


@pytest.mark.asyncio
async def test_truncates_large_response():
    big = b"x" * (MAX_RESPONSE_BYTES + 5000)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big, headers={"content-type": "text/plain"})

    tool = WebFetchTool()
    ctx = _runtime_ctx()
    with patch.object(web_fetch_module, "_is_safe_host", return_value=True), _patched_client(handler):
        events = await _collect(tool, {"url": "https://example.com/big"}, ctx)

    out = _end_payload(events)["output"]
    assert out["success"] is True
    assert out["truncated"] is True
    assert len(out["content"]) <= MAX_TEXT_CHARS


@pytest.mark.asyncio
async def test_skips_non_text_content_type():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x89PNG\r\n", headers={"content-type": "image/png"})

    tool = WebFetchTool()
    ctx = _runtime_ctx()
    with patch.object(web_fetch_module, "_is_safe_host", return_value=True), _patched_client(handler):
        events = await _collect(tool, {"url": "https://example.com/img"}, ctx)

    out = _end_payload(events)["output"]
    assert out["success"] is True
    assert out["content"] is None
    assert "image/png" in out["error_message"]


@pytest.mark.asyncio
async def test_blocks_redirect_to_private_host():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://10.0.0.1/internal"})

    tool = WebFetchTool()
    ctx = _runtime_ctx()
    safe_calls = {"n": 0}

    def fake_safe(host):
        safe_calls["n"] += 1
        # First call: initial host (example.com) -> safe.
        # Subsequent call: redirect target -> unsafe.
        return safe_calls["n"] == 1

    with patch.object(web_fetch_module, "_is_safe_host", side_effect=fake_safe), _patched_client(handler):
        events = await _collect(tool, {"url": "https://example.com/redir"}, ctx)

    out = _end_payload(events)["output"]
    assert out["success"] is False
    assert "redirect" in out["error_message"].lower()
