"""Unit tests for the MCP file-materialization bridge (Workstream A1).

Covers the pure pieces: MIME→extension mapping and pulling file blobs out of an
MCP tool result. The full materialize→File→excel_files path is exercised by the
e2e suite (needs db + report context).
"""
import base64

from app.ai.tools.implementations._file_tool_common import ext_for_mime
from app.data_sources.clients.mcp_client import McpClient


def test_ext_for_mime_known_and_unknown():
    assert ext_for_mime("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet") == "xlsx"
    assert ext_for_mime("text/csv") == "csv"
    assert ext_for_mime("application/pdf") == "pdf"
    assert ext_for_mime("application/json") == "json"
    assert ext_for_mime("TEXT/CSV") == "csv"  # case-insensitive
    assert ext_for_mime("application/x-unknown") is None
    assert ext_for_mime(None) is None


class _Res:
    def __init__(self, blob, mime, uri):
        self.blob = blob
        self.mimeType = mime
        self.uri = uri


class _Block:
    """Minimal stand-in for an MCP content block (only the attrs we read)."""
    def __init__(self, *, resource=None, blob=None, mimeType=None, uri=None, text=None):
        if resource is not None:
            self.resource = resource
        if blob is not None:
            self.blob = blob
        if mimeType is not None:
            self.mimeType = mime = mimeType
        if uri is not None:
            self.uri = uri
        if text is not None:
            self.text = text


class _Result:
    def __init__(self, content):
        self.content = content


def test_extract_binaries_from_embedded_resource():
    b64 = base64.b64encode(b"hello,world\n1,2\n").decode()
    result = _Result([
        _Block(text="some text"),  # ignored — no blob
        _Block(resource=_Res(b64, "text/csv", "drive://file/abc.csv")),
    ])
    bins = McpClient._extract_binaries(result)
    assert len(bins) == 1
    assert bins[0]["blob_b64"] == b64
    assert bins[0]["mime_type"] == "text/csv"
    assert bins[0]["uri"] == "drive://file/abc.csv"


def test_extract_binaries_top_level_blob_and_none():
    b64 = base64.b64encode(b"\x00\x01\x02").decode()
    result = _Result([_Block(blob=b64, mimeType="application/pdf", uri="x://y.pdf")])
    bins = McpClient._extract_binaries(result)
    assert len(bins) == 1 and bins[0]["mime_type"] == "application/pdf"

    # No content / text-only → no binaries.
    assert McpClient._extract_binaries(_Result([])) == []
    assert McpClient._extract_binaries(_Result([_Block(text="hi")])) == []
