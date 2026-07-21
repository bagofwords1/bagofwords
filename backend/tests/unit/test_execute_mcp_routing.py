"""Routing invariants for execute_mcp.

A configured MCP connection is always called over its own wire (no name-based
substitution of a BOW built-in). The only special case is a loopback connection
to this instance, where the HTTP self-call must release the DB transaction first
to avoid SQLite's single-writer deadlock — driven by `_is_loopback_url`.
"""

import pytest

from app.ai.tools.implementations.execute_mcp import _is_loopback_url


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/api/mcp",
        "http://127.0.0.1:3000/api/mcp",
        "http://0.0.0.0:8000/api/mcp",
        "http://[::1]:8000/api/mcp",
        "https://app.localhost/api/mcp",
    ],
)
def test_loopback_urls_detected(url):
    assert _is_loopback_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://eu.bagofwords.com/api/mcp",   # a *different* BOW instance
        "https://mcp.notion.com/mcp",
        "https://my-tableau.example.com/mcp",
        "http://10.0.0.5:8000/api/mcp",         # LAN peer, not this process
        "",
        "not a url",
    ],
)
def test_external_urls_are_not_loopback(url):
    assert _is_loopback_url(url) is False
