"""A successful prefix of a catalog is not a successful catalog."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.data_sources.clients.discovery import IncompleteDiscoveryError
from app.data_sources.clients.mcp_client import McpClient


@pytest.mark.parametrize("pages", [1, 2, 4])
def test_tool_discovery_rejects_truncated_pagination_and_allows_retry(monkeypatch, pages):
    from app.data_sources.clients import mcp_client
    monkeypatch.setattr(mcp_client, "_MAX_TOOL_PAGES", pages)
    complete = False

    class Session:
        async def list_tools(self, **kwargs):
            return SimpleNamespace(tools=[], nextCursor=None if complete else "next-page")

    @asynccontextmanager
    async def connect(self):
        yield Session()

    # The session is the MCP transport boundary; client pagination and common
    # validation both run real.
    monkeypatch.setattr(McpClient, "_connect", connect)
    client = McpClient(server_url="http://provider.invalid")
    with pytest.raises(IncompleteDiscoveryError):
        asyncio.run(client.alist_tools())
    complete = True
    assert asyncio.run(client.alist_tools()) == []


def test_native_tool_discovery_preserves_task_cancellation(monkeypatch):
    @asynccontextmanager
    async def connect(self):
        raise asyncio.CancelledError()
        yield  # pragma: no cover — required for an async context manager
    monkeypatch.setattr(McpClient, "_connect", connect)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(McpClient(server_url="http://provider.invalid").alist_tools())
