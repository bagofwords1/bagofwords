"""Unit tests for the MCP resource helpers (format + connection selection).

The helpers are intentionally dependency-free, so we load them directly by file
path — keeping the test fast and runnable without importing the full app (the
implementations package auto-imports every tool on import).
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

_HELPER = (
    Path(__file__).resolve().parents[2]
    / "app" / "ai" / "tools" / "implementations" / "_mcp_resource_helpers.py"
)
_spec = importlib.util.spec_from_file_location("_mcp_resource_helpers", _HELPER)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
format_resource_contents = _mod.format_resource_contents
select_mcp_connection = _mod.select_mcp_connection


def _conn(id, name):
    return SimpleNamespace(id=id, name=name)


# ---------- format_resource_contents ----------

def test_format_joins_text_blocks():
    content, mime, truncated = format_resource_contents([
        {"type": "text", "text": "line one", "mime_type": "text/markdown"},
        {"type": "text", "text": "line two", "mime_type": "text/plain"},
    ])
    assert content == "line one\nline two"
    assert mime == "text/markdown"  # first block's mime wins
    assert truncated is False


def test_format_binary_is_placeholder_not_inlined():
    content, mime, truncated = format_resource_contents([
        {"type": "binary", "byte_size": 2048, "mime_type": "image/png"},
    ])
    assert content == "[binary resource: image/png, 2048 bytes]"
    assert mime == "image/png"
    assert truncated is False


def test_format_binary_defaults_mime():
    content, _, _ = format_resource_contents([{"type": "binary", "byte_size": 0}])
    assert content == "[binary resource: application/octet-stream, 0 bytes]"


def test_format_truncates_over_cap():
    big = "x" * 100
    content, _, truncated = format_resource_contents(
        [{"type": "text", "text": big}], max_chars=10
    )
    assert truncated is True
    assert content.startswith("x" * 10)
    assert "truncated, 100 total chars" in content


def test_format_empty():
    content, mime, truncated = format_resource_contents([])
    assert content == ""
    assert mime is None
    assert truncated is False


# ---------- select_mcp_connection ----------

def test_select_single_connection_auto():
    c = _conn("id-1", "Pulse")
    assert select_mcp_connection([c], None) is c


def test_select_none_attached_returns_error():
    result = select_mcp_connection([], None)
    assert isinstance(result, str)
    assert "No MCP connections" in result


def test_select_ambiguous_requires_id():
    result = select_mcp_connection([_conn("a", "One"), _conn("b", "Two")], None)
    assert isinstance(result, str)
    assert "specify connection_id" in result
    assert "One (a)" in result and "Two (b)" in result


def test_select_by_id():
    a, b = _conn("a", "One"), _conn("b", "Two")
    assert select_mcp_connection([a, b], "b") is b


def test_select_by_name():
    a, b = _conn("a", "One"), _conn("b", "Two")
    assert select_mcp_connection([a, b], "One") is a


def test_select_unknown_id_returns_error():
    result = select_mcp_connection([_conn("a", "One")], "nope")
    assert isinstance(result, str)
    assert "not found" in result
