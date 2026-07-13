"""read_file content-cache correctness: truncated renders must never be
served from (or written to) the cache.

The failure mode: the first read of a big file renders text clipped at
max_chars and caches it; every later read — even one asking for MORE — then
gets the clipped text back, and the cache-hit path re-materializes the
session file from that clipped render, so downstream analysis (inspect_data /
write_csv) silently operates on a fraction of the file. Complete renders keep
caching as before.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.ai.tools.implementations import _file_cache
from app.ai.tools.implementations.read_file import ReadFileTool


def _long_text(lines: int = 2000) -> str:
    return "".join(f"line-{i} payload\n" for i in range(lines))


class _FakeFileClient:
    """Minimal file client for the tool: async read + cheap version token."""

    def __init__(self, text: str):
        self._text = text
        self.read_calls = 0

    async def aread_file(self, file_id, sheet=None, **_):
        self.read_calls += 1
        return self._text

    async def afile_version(self, file_id):
        return "v1"


async def _run(tool, tool_input):
    return [e async for e in tool.run_stream(tool_input, {})][-1].payload


@pytest.fixture()
def cache_root(tmp_path, monkeypatch):
    monkeypatch.setattr(_file_cache, "_CACHE_ROOT", tmp_path / "filecache")
    return tmp_path / "filecache"


@pytest.mark.asyncio
async def test_truncated_render_is_not_cached(cache_root):
    client = _FakeFileClient(_long_text())
    with patch(
        "app.ai.tools.implementations.read_file.resolve_file_client",
        new=AsyncMock(return_value=(client, None)),
    ):
        payload = await _run(ReadFileTool(), {
            "connection_id": "C1", "file_id": "big.log", "max_chars": 200,
        })
    assert payload["output"]["truncated"] is True
    # Write-side guard: nothing persisted for a clipped render.
    assert _file_cache.read("C1", "big.log", "v1") is None


@pytest.mark.asyncio
async def test_stale_truncated_cache_entry_is_ignored(cache_root):
    full = _long_text()
    # A poisoned entry from before the guard existed: clipped text, same version.
    _file_cache.write(
        "C1", "big.log", "v1",
        rendered={"content_type": "text", "text": full[:200], "truncated": True},
    )
    client = _FakeFileClient(full)
    with patch(
        "app.ai.tools.implementations.read_file.resolve_file_client",
        new=AsyncMock(return_value=(client, None)),
    ):
        payload = await _run(ReadFileTool(), {
            "connection_id": "C1", "file_id": "big.log", "max_chars": 500_000,
        })
    out = payload["output"]
    # Read-side guard: served live and complete, not the clipped cache.
    assert client.read_calls == 1
    assert out["truncated"] is False
    assert out["text"] == full


@pytest.mark.asyncio
async def test_complete_render_still_caches_and_serves(cache_root):
    text = "short and sweet\n"
    client = _FakeFileClient(text)
    with patch(
        "app.ai.tools.implementations.read_file.resolve_file_client",
        new=AsyncMock(return_value=(client, None)),
    ):
        first = await _run(ReadFileTool(), {"connection_id": "C1", "file_id": "a.txt"})
        second = await _run(ReadFileTool(), {"connection_id": "C1", "file_id": "a.txt"})
    assert first["output"]["text"] == text
    assert second["output"]["text"] == text
    # Second call was a cache hit — the source was only read once.
    assert client.read_calls == 1
