"""A successful prefix of a catalog is not a successful catalog."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.data_sources.clients.discovery import IncompleteDiscoveryError
from app.data_sources.clients.mcp_client import McpClient


@pytest.mark.parametrize("pages", [1, 2, 4])
def test_tool_discovery_publishes_the_page_capped_list(monkeypatch, pages):
    """The page cap is a safety limit, not a failure: a server with more pages
    still publishes the tools that were read."""
    from app.data_sources.clients import mcp_client
    monkeypatch.setattr(mcp_client, "_MAX_TOOL_PAGES", pages)
    served = []

    class Session:
        async def list_tools(self, **kwargs):
            name = f"tool_{len(served)}"
            served.append(name)
            tool = SimpleNamespace(name=name, description="", inputSchema={},
                                   outputSchema=None, annotations=None)
            return SimpleNamespace(tools=[tool], nextCursor="next-page")

    @asynccontextmanager
    async def connect(self):
        yield Session()

    monkeypatch.setattr(McpClient, "_connect", connect)
    tools = asyncio.run(McpClient(server_url="http://provider.invalid").alist_tools())
    assert [t["name"] for t in tools] == served
    assert len(served) == pages


def test_native_tool_discovery_preserves_task_cancellation(monkeypatch):
    @asynccontextmanager
    async def connect(self):
        raise asyncio.CancelledError()
        yield  # pragma: no cover — required for an async context manager
    monkeypatch.setattr(McpClient, "_connect", connect)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(McpClient(server_url="http://provider.invalid").alist_tools())


@pytest.mark.parametrize("kind", ["mysql", "clickhouse"])
def test_exhausted_sql_discovery_fallbacks_are_not_successful_empty_catalogs(monkeypatch, kind):
    from contextlib import contextmanager
    from unittest.mock import Mock
    from app.data_sources.clients.mysql_client import MysqlClient
    from app.data_sources.clients.clickhouse_client import ClickhouseClient

    @contextmanager
    def unavailable(self):
        raise ConnectionError("injected driver disconnection")
        yield  # pragma: no cover

    if kind == "mysql":
        client = MysqlClient(host="db.invalid", port=3306, database="fixture")
    else:
        monkeypatch.setattr("app.data_sources.clients.clickhouse_client.clickhouse_connect.get_client", lambda **kw: Mock())
        client = ClickhouseClient(host="db.invalid", port=8123, database="fixture", user="fixture", password="")
    monkeypatch.setattr(type(client), "connect", unavailable)
    with pytest.raises(IncompleteDiscoveryError):
        asyncio.run(client.aget_schemas())


def test_catalog_error_is_resolvable_in_every_locale():
    import json
    from pathlib import Path
    code = IncompleteDiscoveryError().error_code
    catalogs = Path(__file__).resolve().parents[3] / "locales"
    for path in catalogs.glob("*.json"):
        catalog = json.loads(path.read_text())
        assert catalog["errors"].get(code), f"Missing top-level error translation in {path.stem}"


def test_optional_document_content_failure_keeps_complete_file_listing(monkeypatch, tmp_path):
    from app.data_sources.clients.network_dir_client import NetworkDirClient
    (tmp_path / "unreadable.pdf").write_bytes(b"fixture document")
    (tmp_path / "available.txt").write_text("available catalog content")
    def extraction_failure(*args, **kwargs):
        raise ValueError("injected document parser failure")
    monkeypatch.setattr("app.data_sources.clients.network_dir_client.extract_document_text", extraction_failure)
    tables = asyncio.run(NetworkDirClient(root_path=str(tmp_path), index_mode="content").aget_schemas())
    assert {t.name for t in tables} == {"unreadable.pdf", "available.txt"}


def test_validation_is_idempotent_and_consumes_recorded_failures():
    from app.data_sources.clients.discovery import DiscoveryClient, require_complete_discovery
    client = DiscoveryClient()
    client.mark_discovery_incomplete("listing")
    with pytest.raises(IncompleteDiscoveryError):
        require_complete_discovery(client, ["a"])
    # A failure is reported once; it never leaks into the client's next run.
    validated = require_complete_discovery(client, ["a"])
    assert require_complete_discovery(client, validated) is validated


def test_unread_parts_and_entry_types_do_not_block_publication():
    """Unread parts/types keep their existing entries during the prune; the
    rest of the catalog still refreshes, so they are not discovery failures."""
    from app.data_sources.clients.discovery import require_complete_discovery
    client = SimpleNamespace(
        unindexed_table_prefixes=lambda: ["Space/Busy app"],
        unindexed_entry_types=lambda: ["routine"],
    )
    assert require_complete_discovery(client, ["Space/Other app/t"]) == ["Space/Other app/t"]


def test_unreadable_models_are_named_in_the_error():
    from app.data_sources.clients.discovery import require_complete_discovery
    client = SimpleNamespace(discovery_diagnostics=[{"datasetName": "Sales model", "reason": "no Build"}])
    with pytest.raises(IncompleteDiscoveryError, match="Sales model"):
        require_complete_discovery(client, ["Workspace/Other/t"])
