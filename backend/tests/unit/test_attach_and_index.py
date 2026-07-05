"""Unit tests for attach_file and the index-first search path."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.tools.implementations.attach_file import AttachFileTool
from app.ai.tools.implementations.search_files import SearchFilesTool, _index_search
from app.data_sources.clients.base import Capability


def _run(agen_factory):
    async def _collect():
        return [ev async for ev in agen_factory()]
    return asyncio.run(_collect())


def _end(events):
    return events[-1].payload["output"]


def _ctx():
    return {
        "db": AsyncMock(),
        "organization": MagicMock(id="org-1"),
        "report": MagicMock(data_sources=[]),
        "user": MagicMock(id="user-1"),
    }


def _row(name, keywords=None, file_id=None):
    """A fake catalog Table row."""
    meta = {"network_dir": {"file_id": file_id or name}}
    if keywords is not None:
        meta["network_dir"]["keywords"] = keywords
    return SimpleNamespace(name=name, metadata_json=meta)


# ------------------------------------------------------ index search


class TestIndexSearch:
    def test_matches_keyword(self):
        rows = [
            _row("contracts/acme.csv", ["acme", "arbitration", "payment"]),
            _row("contracts/globex.csv", ["globex", "renewal"]),
        ]
        hits = _index_search(rows, "arbitration", 10)
        assert [h["path"] for h in hits] == ["contracts/acme.csv"]

    def test_ranks_filename_over_keyword(self):
        rows = [
            _row("notes/acme.md", ["misc"]),               # name match (strong)
            _row("contracts/x.csv", ["acme", "clause"]),   # keyword match
        ]
        hits = _index_search(rows, "acme", 10)
        assert hits[0]["path"] == "notes/acme.md"

    def test_empty_when_not_indexed(self):
        # rows carry NO keywords -> not indexed -> [] (caller falls back to live)
        rows = [_row("a.csv"), _row("b.csv")]
        assert _index_search(rows, "anything", 10) == []


# ------------------------------------------------------ attach_file


class TestAttachFileRegistration:
    def test_in_all(self):
        from app.ai.tools.implementations import __all__
        assert "AttachFileTool" in __all__

    def test_metadata(self):
        m = AttachFileTool().metadata
        assert m.name == "attach_file"
        assert m.category == "action"
        assert m.requires_capability == "read_file"
        assert "attach" in m.tags


class TestAttachFileRun:
    def test_happy_path_attaches(self):
        client = MagicMock()
        client.read_raw_bytes = MagicMock(return_value=(b"%PDF-1.4 data", "acme.pdf", "application/pdf"))

        tool = AttachFileTool()
        with patch(
            "app.ai.tools.implementations.attach_file.resolve_file_client",
            new=AsyncMock(return_value=(client, None)),
        ), patch.object(
            AttachFileTool, "_persist_durable", new=AsyncMock(return_value="FILE-123")
        ):
            events = _run(lambda: tool.run_stream(
                {"connection_id": "C1", "file_ids": ["contracts/acme.pdf"]}, _ctx()))
        out = _end(events)
        assert out["success"] is True
        assert out["attached_count"] == 1
        assert out["files"][0]["session_file_id"] == "FILE-123"
        assert out["files"][0]["name"] == "acme.pdf"

    def test_partial_failure_reported(self):
        client = MagicMock()
        # first file ok, second raises
        client.read_raw_bytes = MagicMock(side_effect=[
            (b"data", "ok.pdf", "application/pdf"),
            ValueError("File not found: bad.pdf"),
        ])
        tool = AttachFileTool()
        with patch(
            "app.ai.tools.implementations.attach_file.resolve_file_client",
            new=AsyncMock(return_value=(client, None)),
        ), patch.object(
            AttachFileTool, "_persist_durable", new=AsyncMock(return_value="FILE-1")
        ):
            events = _run(lambda: tool.run_stream(
                {"connection_id": "C1", "file_ids": ["ok.pdf", "bad.pdf"]}, _ctx()))
        out = _end(events)
        assert out["attached_count"] == 1
        errs = [f for f in out["files"] if f.get("error")]
        assert len(errs) == 1 and "bad.pdf" in errs[0]["file_id"]

    def test_readonly_capability_error(self):
        with patch(
            "app.ai.tools.implementations.attach_file.resolve_file_client",
            new=AsyncMock(return_value=(None, "Connection does not support read_file.")),
        ):
            events = _run(lambda: AttachFileTool().run_stream(
                {"connection_id": "C1", "file_ids": ["a.pdf"]}, _ctx()))
        out = _end(events)
        assert out["success"] is False
        assert "read_file" in out["error"]


class TestAttachCatalogGating:
    def test_gated_on_read_file_capability(self):
        from app.ai.registry import ToolRegistry
        cat = ToolRegistry().get_catalog_for_plan_type(
            "action", None, available_capabilities={"read_file"})
        assert "attach_file" in {t["name"] for t in cat}
        cat2 = ToolRegistry().get_catalog_for_plan_type(
            "action", None, available_capabilities={"query"})
        assert "attach_file" not in {t["name"] for t in cat2}
