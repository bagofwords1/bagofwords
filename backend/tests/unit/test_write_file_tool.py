"""Unit tests for the write_file agent tool.

Covers registration, metadata/capability gating, input validation (exactly one
content source), the happy path with a stubbed writable client, and the
read-only rejection path.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.tools.implementations.write_file import WriteFileTool
from app.data_sources.clients.base import Capability


def _run(tool, tool_input, runtime_ctx):
    async def _collect():
        events = []
        async for ev in tool.run_stream(tool_input, runtime_ctx):
            events.append(ev)
        return events
    return asyncio.run(_collect())


def _end_output(events):
    end = events[-1]
    return end.payload["output"]


# --------------------------------------------------- registration / metadata


class TestRegistration:
    def test_in_implementations_all(self):
        from app.ai.tools.implementations import __all__
        assert "WriteFileTool" in __all__

    def test_metadata(self):
        m = WriteFileTool().metadata
        assert m.name == "write_file"
        assert m.category == "action"
        assert m.idempotent is False
        assert m.requires_capability == "write_file"
        assert "write" in m.tags


class TestCatalogGating:
    def test_excluded_without_write_capability(self):
        from app.ai.registry import ToolRegistry
        catalog = ToolRegistry().get_catalog_for_plan_type(
            "action", None,
            available_capabilities={"query", "list_files", "read_file", "search_files"},
        )
        assert "write_file" not in {t["name"] for t in catalog}

    def test_included_with_write_capability(self):
        from app.ai.registry import ToolRegistry
        catalog = ToolRegistry().get_catalog_for_plan_type(
            "action", None,
            available_capabilities={"write_file"},
        )
        assert "write_file" in {t["name"] for t in catalog}


# --------------------------------------------------------- run_stream


def _ctx():
    return {
        "db": AsyncMock(),
        "organization": MagicMock(id="org-1"),
        "report": MagicMock(data_sources=[]),
        "user": MagicMock(id="user-1"),
    }


class TestRunStream:
    def test_requires_exactly_one_content_source(self):
        # both provided -> error, no client resolution attempted
        events = _run(WriteFileTool(), {
            "connection_id": "C1", "filename": "a.txt",
            "content": "x", "source_file_id": "F1",
        }, _ctx())
        out = _end_output(events)
        assert out["success"] is False
        assert "exactly one" in out["error"].lower()

    def test_requires_at_least_one_content_source(self):
        events = _run(WriteFileTool(), {
            "connection_id": "C1", "filename": "a.txt",
        }, _ctx())
        out = _end_output(events)
        assert out["success"] is False
        assert "exactly one" in out["error"].lower()

    def test_happy_path_writes_content(self):
        fake_client = AsyncMock()
        fake_client.capabilities = {Capability.WRITE_FILE}
        fake_client.awrite_file = AsyncMock(return_value={
            "id": "related/a.txt", "name": "a.txt", "path": "related/a.txt",
            "mime_type": "text/plain", "size": 5, "modified_at": None, "web_url": "file:///x",
        })
        with patch(
            "app.ai.tools.implementations.write_file.resolve_file_client",
            new=AsyncMock(return_value=(fake_client, None)),
        ):
            events = _run(WriteFileTool(), {
                "connection_id": "C1", "filename": "a.txt",
                "folder": "related", "content": "hello",
            }, _ctx())
        out = _end_output(events)
        assert out["success"] is True
        assert out["file"]["id"] == "related/a.txt"
        fake_client.awrite_file.assert_awaited_once()
        # folder + overwrite are threaded through
        _, kwargs = fake_client.awrite_file.call_args
        assert kwargs["folder_id"] == "related"
        assert kwargs["overwrite"] is False

    def test_readonly_connection_rejected(self):
        # resolve_file_client returns the capability error for a read-only source
        err = "Connection 'Docs' does not support write_file."
        with patch(
            "app.ai.tools.implementations.write_file.resolve_file_client",
            new=AsyncMock(return_value=(None, err)),
        ):
            events = _run(WriteFileTool(), {
                "connection_id": "C1", "filename": "a.txt", "content": "x",
            }, _ctx())
        out = _end_output(events)
        assert out["success"] is False
        assert "write_file" in out["error"]

    def test_client_error_surfaced(self):
        fake_client = AsyncMock()
        fake_client.capabilities = {Capability.WRITE_FILE}
        fake_client.awrite_file = AsyncMock(side_effect=ValueError("File already exists: a.txt"))
        with patch(
            "app.ai.tools.implementations.write_file.resolve_file_client",
            new=AsyncMock(return_value=(fake_client, None)),
        ):
            events = _run(WriteFileTool(), {
                "connection_id": "C1", "filename": "a.txt", "content": "x",
            }, _ctx())
        out = _end_output(events)
        assert out["success"] is False
        assert "already exists" in out["error"]
