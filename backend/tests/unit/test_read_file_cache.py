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

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.ai.tools.implementations import _file_cache
from app.ai.tools.implementations.read_file import ReadFileTool


def _long_text(lines: int = 2000) -> str:
    return "".join(f"line-{i} payload\n" for i in range(lines))


class _FakeFileClient:
    """Minimal file client for the tool: async read + cheap version token."""

    def __init__(self, text: str, connection_id: str = "C1"):
        self._text = text
        self.read_calls = 0
        # resolve_file_client stamps the connection it resolved on the client.
        self._bow_connection = SimpleNamespace(id=connection_id)

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


@pytest.mark.asyncio
async def test_cache_follows_the_resolved_connection_not_the_id_the_caller_typed(cache_root):
    """Any string resolves to one of the run's own connections, so the string
    itself must never pick the cache entry: two different connections asked
    for with the same id do not share content, and one connection asked for
    under two different ids does."""
    first = _FakeFileClient("content of the first connection\n", connection_id="conn-one")
    second = _FakeFileClient("content of the second connection\n", connection_id="conn-two")

    async def _read(client, typed_id):
        with patch(
            "app.ai.tools.implementations.read_file.resolve_file_client",
            new=AsyncMock(return_value=(client, None)),
        ):
            return (await _run(ReadFileTool(), {"connection_id": typed_id, "file_id": "a.txt"}))["output"]["text"]

    assert await _read(first, "conn-one") == "content of the first connection\n"
    # Same typed id, same file id and version, but a different connection.
    assert await _read(second, "conn-one") == "content of the second connection\n"
    assert second.read_calls == 1
    # The first connection under another name is still a cache hit.
    assert await _read(first, "its display name") == "content of the first connection\n"
    assert first.read_calls == 1


@pytest.mark.parametrize("owner", ["../../outside", "/abs/elsewhere", "a/b", "..", "x" * 300])
def test_cache_entries_stay_under_the_cache_root(cache_root, owner):
    _file_cache.write(owner, "a.txt", "v1", rendered={"content_type": "text", "text": "kept"})
    assert _file_cache.read(owner, "a.txt", "v1")["rendered"]["text"] == "kept"
    root = cache_root.resolve()
    written = [p.resolve() for p in cache_root.parent.rglob("meta.json")]
    assert written and all(root in p.parents for p in written)
    # Exactly <root>/<owner dir>/<entry dir>/meta.json: no extra path segments.
    assert all(len(p.relative_to(root).parts) == 3 for p in written)
