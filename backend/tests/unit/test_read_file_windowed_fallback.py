"""read_file offset/length paging on connectors WITHOUT a native ranged read.

Only network_dir and S3 answer `offset`/`length` themselves (a raw byte
window). Every other file client (SharePoint, Graph drives, Google Drive,
Documentum, mailboxes, notes) ignores the kwargs and returns the whole file —
the tool must then page the text a whole-file read would show, in characters,
under the SAME window contract (content / next_cursor / eof / unit).

The contract under test, independent of the client type:
- the concatenation of all windows is exactly the text a whole-file read shows;
- every window is at most `length` long, `next_cursor` strictly advances and
  is null only on the `eof` window;
- an offset at or past the end is an empty window at eof, not an error;
- a binary with no text layer is an error that names the way out;
- a client with a native window keeps using it (unit "bytes").
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from app.ai.tools.implementations.read_file import ReadFileTool
from app.data_sources.clients._file_source_common import DocumentText, NamedBytes
from app.data_sources.clients.base import Capability

_RESOLVE = "app.ai.tools.implementations.read_file.resolve_file_client"


async def _run(client, inputs: dict[str, Any], ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    """Drive the tool through its public surface and return the final payload
    (output + observation), with the connection resolver patched to the fake
    client — no DB, no network."""
    with patch(_RESOLVE, new=AsyncMock(return_value=(client, None))):
        events = [e async for e in ReadFileTool().run_stream(
            {"connection_id": "C1", **inputs}, ctx if ctx is not None else {}
        )]
    return events[-1].payload


def _whole_file_client(payload: Any) -> MagicMock:
    """A connector that ignores offset/length entirely (SharePoint, Graph,
    Drive, …): every read returns the whole payload."""
    client = MagicMock()
    client.capabilities = {Capability.READ_FILE}
    client.aread_file = AsyncMock(return_value=payload)
    return client


async def _page_to_eof(client, file_id: str, length: int, **extra) -> list[dict[str, Any]]:
    outputs = []
    offset = 0
    for _ in range(1000):  # bounded so a broken cursor can't spin forever
        payload = await _run(client, {"file_id": file_id, "offset": offset, "length": length, **extra})
        out = payload["output"]
        assert out["success"], out.get("error")
        outputs.append(out)
        if out["eof"]:
            break
        offset = out["next_cursor"]
    else:
        pytest.fail("paging never reached eof")
    return outputs


_LONG_TEXT = "\n".join(f"line {i:04d}: " + "word " * (i % 7) for i in range(400))  # ~15k chars


@pytest.mark.parametrize("payload,file_id", [
    pytest.param(_LONG_TEXT, "notes/long.txt", id="str"),
    pytest.param(DocumentText(_LONG_TEXT, raw=b"%PDF-1.4 fake", name="report.pdf", mime="application/pdf"),
                 "01OPAQUEGRAPHID", id="DocumentText"),
    pytest.param(pd.DataFrame({"region": [f"region-{i:05d}" for i in range(1200)], "amount": list(range(1200))}),
                 "sales.xlsx", id="DataFrame"),
    pytest.param({"items": [{"id": i, "name": f"item {i}", "tags": ["a", "b"]} for i in range(200)]},
                 "data.json", id="dict"),
])
@pytest.mark.parametrize("length", [1500, 7000])
@pytest.mark.asyncio
async def test_windows_concatenate_to_the_whole_file_read(payload, file_id, length):
    client = _whole_file_client(payload)

    # What a NORMAL (non-windowed) read shows for this payload is the
    # reference the windows must reproduce — same tool, same client.
    whole = (await _run(client, {"file_id": file_id, "max_chars": 500_000, "max_rows": 100_000}))["output"]
    assert whole["success"] and not whole["truncated"]
    reference = whole["csv"] if whole["content_type"] == "tabular" else whole["text"]
    assert len(reference) > length  # the case genuinely needs several pages

    pages = await _page_to_eof(client, file_id, length)

    assert len(pages) > 1
    assert "".join(p["text"] for p in pages) == reference
    for p in pages:
        assert p["windowed"] is True and p["unit"] == "chars" and p["encoding"] == "text"
        assert p["content_type"] == "text"
        assert len(p["text"]) <= length
        assert p["total_size"] == len(reference)
    cursors = [p["next_cursor"] for p in pages[:-1]]
    assert cursors == sorted(cursors) and len(set(cursors)) == len(cursors)
    assert all(c is not None and c > 0 for c in cursors)
    assert pages[-1]["eof"] is True and pages[-1]["next_cursor"] is None
    assert all(p["eof"] is False for p in pages[:-1])


@pytest.mark.asyncio
async def test_window_content_reaches_the_observation():
    """The planner reads the observation, not the output: the window text and
    the cursor to continue from must be in observation.details."""
    client = _whole_file_client(DocumentText(_LONG_TEXT, raw=b"x", name="long.docx"))
    payload = await _run(client, {"file_id": "DOC1", "offset": 0, "length": 3000})
    obs = payload["observation"]
    assert obs["success"]
    assert payload["output"]["text"] in obs["details"]
    assert str(payload["output"]["next_cursor"]) in obs["details"]
    assert "chars" in obs["summary"]


@pytest.mark.asyncio
async def test_windows_default_to_a_large_page_when_length_is_omitted():
    client = _whole_file_client("short text\nsecond line\n")
    out = (await _run(client, {"file_id": "t.txt", "offset": 0}))["output"]
    assert out["success"] and out["eof"] is True and out["next_cursor"] is None
    assert out["text"] == "short text\nsecond line\n"


@pytest.mark.parametrize("offset", [len(_LONG_TEXT), len(_LONG_TEXT) + 1, len(_LONG_TEXT) * 3])
@pytest.mark.asyncio
async def test_offset_at_or_past_the_end_is_an_empty_eof_window(offset):
    client = _whole_file_client(_LONG_TEXT)
    out = (await _run(client, {"file_id": "long.txt", "offset": offset, "length": 100}))["output"]
    assert out["success"] is True
    assert out["text"] == "" and out["eof"] is True and out["next_cursor"] is None
    assert out["total_size"] == len(_LONG_TEXT)


@pytest.mark.asyncio
async def test_binary_without_a_text_layer_is_an_error_that_names_the_way_out():
    client = _whole_file_client(NamedBytes(b"\x89PNG\r\n\x1a\n...", name="scan.png", mime="image/png"))
    out = (await _run(client, {"file_id": "IMG1", "offset": 0, "length": 100}))["output"]
    assert out["success"] is False
    assert out.get("windowed") is not True and "text" not in out
    assert "as_images" in out["error"] and "attach_file" in out["error"]


@pytest.mark.asyncio
async def test_native_byte_window_is_still_used_when_the_client_has_one():
    """network_dir / S3 answer offset/length themselves — the tool must keep
    that route (raw bytes, unit 'bytes'), not re-read and re-slice."""
    client = MagicMock()
    client.capabilities = {Capability.READ_FILE}

    async def aread_file(file_id, **kw):
        if kw.get("offset") is None:
            raise AssertionError("whole-file read must not happen when a native window exists")
        return {"content": "chunk-of-bytes\n", "encoding": "text", "offset": kw["offset"],
                "length": 15, "next_cursor": kw["offset"] + 15, "total_size": 100, "eof": False}
    client.aread_file = aread_file

    out = (await _run(client, {"file_id": "logs/app.log", "offset": 30, "length": 15}))["output"]
    assert out["success"] and out["windowed"] is True
    assert out["unit"] == "bytes" and out["byte_count"] == 15
    assert out["text"] == "chunk-of-bytes\n" and out["next_cursor"] == 45 and out["eof"] is False


@pytest.mark.asyncio
async def test_native_window_client_pages_a_document_as_extracted_text():
    """A byte window of a PDF/DOCX is a slice of the container (base64, or
    PDF/OOXML syntax that happens to decode) — useless to the model. On a
    native-window client (network_dir, chat uploads) a document must be paged
    as its extracted text instead."""
    client = MagicMock()
    client.capabilities = {Capability.READ_FILE}

    async def aread_file(file_id, **kw):
        if kw.get("offset") is not None:
            return {"content": "%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>", "encoding": "text",
                    "offset": kw["offset"], "length": 36, "next_cursor": kw["offset"] + 36,
                    "total_size": 90_000, "eof": False}
        return _LONG_TEXT  # the whole-file read extracts the text
    client.aread_file = aread_file

    pages = await _page_to_eof(client, "docs/contract.pdf", 4000)
    assert "".join(p["text"] for p in pages) == _LONG_TEXT
    assert all(p["unit"] == "chars" and p["encoding"] == "text" for p in pages)
    assert "page_range" in (await _run(client, {"file_id": "docs/contract.pdf", "offset": 0, "length": 4000}))["observation"]["summary"]


@pytest.mark.asyncio
async def test_native_base64_window_of_a_real_binary_stays_binary():
    """The document escalation must not swallow genuinely binary windows: a
    byte window of a .bin / image keeps the native base64 contract."""
    client = MagicMock()
    client.capabilities = {Capability.READ_FILE}
    client.aread_file = AsyncMock(return_value={
        "content": "AAEC", "encoding": "base64", "offset": 0, "length": 3,
        "next_cursor": 3, "total_size": 10, "eof": False,
    })
    out = (await _run(client, {"file_id": "blobs/firmware.bin", "offset": 0, "length": 3}))["output"]
    assert out["success"] and out["unit"] == "bytes" and out["encoding"] == "base64"
    assert out["content_type"] == "binary" and out["text"] == "AAEC"


@pytest.mark.asyncio
async def test_json_pages_are_the_same_dump_a_whole_read_shows():
    data = {"k": ["v" * 50] * 100, "unicode": "שלום"}
    client = _whole_file_client(data)
    whole = (await _run(client, {"file_id": "d.json", "max_chars": 500_000}))["output"]
    pages = await _page_to_eof(client, "d.json", 1000)
    joined = "".join(p["text"] for p in pages)
    assert joined == whole["text"]
    assert json.loads(joined) == data


@pytest.mark.asyncio
async def test_session_upload_document_pages_as_extracted_text(tmp_path):
    """Chat uploads are served by SessionFileClient over NetworkDirClient, whose
    native window is bytes. A rich document uploaded to the chat must page as
    extracted text (unit chars), a plain text upload as native bytes."""
    import zipfile

    # A minimal but valid DOCX (the same DOC_EXTS extraction path as pptx);
    # authored by hand because no docx library is a dependency here.
    lines = [f"Paragraph {i}: " + "lorem ipsum " * 12 for i in range(60)]
    body = "".join(f"<w:p><w:r><w:t xml:space=\"preserve\">{ln}</w:t></w:r></w:p>" for ln in lines)
    docx = tmp_path / "sf1_report.docx"
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml",
                   '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   f'<w:body>{body}</w:body></w:document>')
    txt = tmp_path / "sf2_notes.txt"
    txt.write_text(_LONG_TEXT, encoding="utf-8")

    doc_file = MagicMock(id="11111111-1111-4111-8111-111111111111", path=str(docx),
                         filename="report.docx", organization_id=None)
    txt_file = MagicMock(id="22222222-2222-4222-8222-222222222222", path=str(txt),
                         filename="notes.txt", organization_id=None)
    ctx = {"report": MagicMock(files=[doc_file, txt_file]), "organization": None}

    async def page(file_id, length):
        outputs, offset = [], 0
        for _ in range(500):
            events = [e async for e in ReadFileTool().run_stream(
                {"connection_id": "", "file_id": file_id, "offset": offset, "length": length}, ctx)]
            out = events[-1].payload["output"]
            assert out["success"], out.get("error")
            outputs.append(out)
            if out["eof"]:
                return outputs
            offset = out["next_cursor"]
        pytest.fail("never reached eof")

    whole = [e async for e in ReadFileTool().run_stream(
        {"connection_id": "", "file_id": doc_file.id, "max_chars": 500_000}, ctx)][-1].payload["output"]
    assert whole["content_type"] == "text" and "Paragraph 59" in whole["text"]

    doc_pages = await page(doc_file.id, 2000)
    assert len(doc_pages) > 1 and all(p["unit"] == "chars" for p in doc_pages)
    assert "".join(p["text"] for p in doc_pages) == whole["text"]

    txt_pages = await page(txt_file.id, 2000)
    assert len(txt_pages) > 1 and all(p["unit"] == "bytes" for p in txt_pages)
    assert "".join(p["text"] for p in txt_pages) == _LONG_TEXT
