"""Unit tests for the list_files / read_file / search_files agent tools.

Covers:
- Tool registration via auto-discovery
- Metadata shape / required fields
- render_file_payload across DataFrame, str, dict/list, bytes branches
- run_stream happy-path with a stubbed client (no DB / no network)
- run_stream error path when capability is missing
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from app.ai.tools.implementations._file_tool_common import render_file_payload
from app.ai.tools.implementations.list_files import ListFilesTool
from app.ai.tools.implementations.read_file import ReadFileTool
from app.ai.tools.implementations.search_files import SearchFilesTool
from app.data_sources.clients.base import Capability


# ----------------------------------------------------- registration


class TestToolRegistration:
    def test_all_three_in_implementations_all(self):
        from app.ai.tools.implementations import __all__

        for name in ("ListFilesTool", "ReadFileTool", "SearchFilesTool"):
            assert name in __all__

    def test_metadata_basics(self):
        for cls, expected in [
            (ListFilesTool, "list_files"),
            (ReadFileTool, "read_file"),
            (SearchFilesTool, "search_files"),
        ]:
            m = cls().metadata
            assert m.name == expected
            assert m.category == "research"
            assert m.idempotent is True
            assert "files" in m.tags

    def test_each_tool_declares_required_capability(self):
        """Capability gating: each file tool must declare the capability its
        backing client needs to expose. Used by the catalog filter to hide
        these tools from agents with no file-source connection attached."""
        for cls, cap in [
            (ListFilesTool, "list_files"),
            (ReadFileTool, "read_file"),
            (SearchFilesTool, "search_files"),
        ]:
            assert cls().metadata.requires_capability == cap


class TestCatalogCapabilityGating:
    """Catalog filter must exclude file tools when no attached connection
    exposes the capability, and include them when one does. Future tools that
    declare requires_capability slot into the same gate."""

    def test_excluded_when_no_file_capability(self):
        from app.ai.registry import ToolRegistry
        catalog = ToolRegistry().get_catalog_for_plan_type(
            "research", None, available_capabilities={"query"},
        )
        names = {t["name"] for t in catalog}
        assert "list_files" not in names
        assert "read_file" not in names
        assert "search_files" not in names

    def test_included_when_capability_present(self):
        from app.ai.registry import ToolRegistry
        catalog = ToolRegistry().get_catalog_for_plan_type(
            "research", None,
            available_capabilities={"query", "list_files", "read_file", "search_files"},
        )
        names = {t["name"] for t in catalog}
        assert "list_files" in names
        assert "read_file" in names
        assert "search_files" in names

    def test_no_filter_passes_through_for_backwards_compat(self):
        """Legacy callers that don't pass available_capabilities should still
        see all tools (no filter = no gating)."""
        from app.ai.registry import ToolRegistry
        catalog = ToolRegistry().get_catalog_for_plan_type("research", None)
        names = {t["name"] for t in catalog}
        assert "list_files" in names
        assert "read_file" in names


class TestResolveFileClientIdResolution:
    """Regression: the LLM often passes a DataSource (agent) ID instead of
    the Connection ID — the resolver must accept either, looking up the
    file-source connection on the data source when needed."""

    def _make_ctx(self, db_mock, ds_id: str, conn_id: str, conn_type: str = "onedrive"):
        # Minimal runtime_ctx with a report containing a data source whose
        # connections include one file-source connection. Mirrors what
        # construct_client / get_user_data_source_schema feed in.
        from unittest.mock import MagicMock

        conn = MagicMock()
        conn.id = conn_id
        conn.type = conn_type
        conn.name = "OneDrive"

        ds = MagicMock()
        ds.id = ds_id
        ds.connections = [conn]

        report = MagicMock()
        report.data_sources = [ds]

        org = MagicMock()
        org.id = "org-1"

        return {
            "db": db_mock,
            "organization": org,
            "report": report,
            "user": MagicMock(id="user-1"),
        }, conn

    def test_accepts_connection_id_direct(self):
        import asyncio
        from unittest.mock import AsyncMock, patch
        from app.ai.tools.implementations._file_tool_common import resolve_file_client
        from app.data_sources.clients.base import Capability

        db = AsyncMock()
        ctx, conn = self._make_ctx(db, ds_id="DS-1", conn_id="CONN-1")

        fake_client = AsyncMock()
        fake_client.capabilities = {Capability.LIST_FILES}
        with patch("app.services.connection_service.ConnectionService") as svc_cls:
            svc_cls.return_value.construct_client = AsyncMock(return_value=fake_client)
            client, err = asyncio.run(resolve_file_client(ctx, "CONN-1", Capability.LIST_FILES))
        assert err is None
        assert client is fake_client

    def test_accepts_data_source_id_as_alias(self):
        """The actual bug from production: agent passed the data_source.id,
        resolver was strict about connection_id and rejected it. After fix,
        the data_source.id resolves to its file-source connection."""
        import asyncio
        from unittest.mock import AsyncMock, patch
        from app.ai.tools.implementations._file_tool_common import resolve_file_client
        from app.data_sources.clients.base import Capability

        db = AsyncMock()
        ctx, conn = self._make_ctx(db, ds_id="DS-1", conn_id="CONN-1")

        fake_client = AsyncMock()
        fake_client.capabilities = {Capability.READ_FILE}
        with patch("app.services.connection_service.ConnectionService") as svc_cls:
            svc_cls.return_value.construct_client = AsyncMock(return_value=fake_client)
            # Pass DS-1 (data_source id) where Connection ID is expected.
            client, err = asyncio.run(resolve_file_client(ctx, "DS-1", Capability.READ_FILE))
        assert err is None, f"Should accept data_source_id as alias, got: {err}"
        assert client is fake_client

    def test_rejects_unrelated_id_with_helpful_error(self):
        import asyncio
        from unittest.mock import AsyncMock
        from app.ai.tools.implementations._file_tool_common import resolve_file_client
        from app.data_sources.clients.base import Capability

        db = AsyncMock()
        ctx, _ = self._make_ctx(db, ds_id="DS-1", conn_id="CONN-1")
        client, err = asyncio.run(resolve_file_client(ctx, "TOTALLY-WRONG", Capability.LIST_FILES))
        assert client is None
        assert "TOTALLY-WRONG" in err
        # Error should hint at what IS attached.
        assert "CONN-1" in err


# --------------------------------------------------- render helper


class TestRenderFilePayload:
    def test_dataframe_to_csv(self):
        df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
        out = render_file_payload("data.csv", df, max_rows=10, max_chars=1000)
        assert out["content_type"] == "tabular"
        assert "a,b" in out["csv"]
        assert out["row_count"] == 3
        assert out["col_count"] == 2
        assert out["truncated"] is False

    def test_dataframe_truncation(self):
        df = pd.DataFrame({"a": range(100)})
        out = render_file_payload("big.csv", df, max_rows=5, max_chars=1000)
        assert out["row_count"] == 5
        assert out["truncated"] is True

    def test_text(self):
        out = render_file_payload("notes.md", "hello world", max_rows=1, max_chars=1000)
        assert out["content_type"] == "text"
        assert out["text"] == "hello world"
        assert out["truncated"] is False

    def test_text_truncated(self):
        out = render_file_payload("notes.md", "x" * 1000, max_rows=1, max_chars=100)
        assert out["truncated"] is True
        assert len(out["text"]) == 100

    def test_json(self):
        out = render_file_payload("config.json", {"k": "v", "n": 1}, max_rows=1, max_chars=1000)
        assert out["content_type"] == "json"
        assert '"k"' in out["text"]

    def test_binary(self):
        out = render_file_payload("file.bin", b"\x00\x01\x02", max_rows=1, max_chars=1000)
        assert out["content_type"] == "binary"
        assert out["byte_count"] == 3


# ---------------------------------------------- run_stream behaviour


async def _collect(stream: AsyncIterator) -> list:
    return [e async for e in stream]


def _patch_resolve(client):
    """Patch resolve_file_client to return (client, None) without touching DB.

    The function is imported by-name into each tool module, so we patch each
    importer's binding.
    """
    targets = (
        "app.ai.tools.implementations.list_files.resolve_file_client",
        "app.ai.tools.implementations.read_file.resolve_file_client",
        "app.ai.tools.implementations.search_files.resolve_file_client",
    )
    return [patch(t, new=AsyncMock(return_value=(client, None))) for t in targets]


@pytest.mark.asyncio
async def test_list_files_glob_filter():
    """name_pattern post-filters via fnmatch (case-insensitive)."""
    fake_client = MagicMock()
    fake_client.capabilities = {Capability.LIST_FILES}
    fake_client.alist_files = AsyncMock(return_value=[
        {"id": "1", "name": "Book 7.xlsx"},
        {"id": "2", "name": "Notes.txt"},
        {"id": "3", "name": "Book 8.xlsx"},
        {"id": "4", "name": "report.pdf"},
    ])
    with _patch_resolve(fake_client)[0]:
        tool = ListFilesTool()
        events = await _collect(tool.run_stream(
            {"connection_id": "DS1", "name_pattern": "*.xlsx"}, {}
        ))
    out = events[-1].payload["output"]
    assert out["success"] is True
    assert {f["name"] for f in out["files"]} == {"Book 7.xlsx", "Book 8.xlsx"}


@pytest.mark.asyncio
async def test_list_files_happy_path():
    fake_client = MagicMock()
    fake_client.capabilities = {Capability.LIST_FILES}
    fake_client.alist_files = AsyncMock(return_value=[
        {"id": "F1", "name": "a.csv", "path": "a.csv", "mime_type": "text/csv",
         "size": 10, "modified_at": "2025-01-01", "web_url": "u"},
        {"id": "F2", "name": "b.xlsx", "path": "b.xlsx",
         "mime_type": "application/vnd.openxmlformats", "size": 200,
         "modified_at": "2025-01-02", "web_url": "u2"},
    ])
    with _patch_resolve(fake_client)[0]:
        tool = ListFilesTool()
        events = await _collect(tool.run_stream({"connection_id": "DS1"}, {}))
    end = events[-1].payload
    assert end["output"]["success"] is True
    assert end["output"]["file_count"] == 2
    assert {f["id"] for f in end["output"]["files"]} == {"F1", "F2"}


@pytest.mark.asyncio
async def test_list_files_resolve_error():
    targets = ("app.ai.tools.implementations.list_files.resolve_file_client",)
    with patch(targets[0], new=AsyncMock(return_value=(None, "boom"))):
        tool = ListFilesTool()
        events = await _collect(tool.run_stream({"connection_id": "DS1"}, {}))
    end = events[-1].payload
    assert end["output"]["success"] is False
    assert end["output"]["error"] == "boom"


@pytest.mark.asyncio
async def test_read_file_happy_path_csv():
    df = pd.DataFrame({"col": [1, 2, 3]})
    fake_client = MagicMock()
    fake_client.capabilities = {Capability.READ_FILE}
    fake_client.aread_file = AsyncMock(return_value=df)
    with _patch_resolve(fake_client)[1]:
        tool = ReadFileTool()
        events = await _collect(tool.run_stream(
            {"connection_id": "DS1", "file_id": "F1"}, {}
        ))
    out = events[-1].payload["output"]
    assert out["success"] is True
    assert out["content_type"] == "tabular"
    assert out["row_count"] == 3
    assert "col" in out["csv"]


@pytest.mark.asyncio
async def test_read_file_handles_client_error():
    fake_client = MagicMock()
    fake_client.capabilities = {Capability.READ_FILE}
    fake_client.aread_file = AsyncMock(side_effect=ValueError("404 from Graph"))
    with _patch_resolve(fake_client)[1]:
        tool = ReadFileTool()
        events = await _collect(tool.run_stream(
            {"connection_id": "DS1", "file_id": "F1"}, {}
        ))
    out = events[-1].payload["output"]
    assert out["success"] is False
    assert "404" in out["error"]


@pytest.mark.asyncio
async def test_search_files_happy_path():
    fake_client = MagicMock()
    fake_client.capabilities = {Capability.SEARCH_FILES}
    fake_client.asearch_files = AsyncMock(return_value=[
        {"id": "F9", "name": "pipeline.xlsx", "mime_type": "x", "size": 1,
         "modified_at": "2025", "web_url": "u"},
    ])
    with _patch_resolve(fake_client)[2]:
        tool = SearchFilesTool()
        events = await _collect(tool.run_stream(
            {"connection_id": "DS1", "query": "pipeline"}, {}
        ))
    out = events[-1].payload["output"]
    assert out["success"] is True
    assert out["file_count"] == 1
    assert out["files"][0]["name"] == "pipeline.xlsx"
