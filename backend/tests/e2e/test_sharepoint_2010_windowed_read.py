"""read_file offset/length paging on SharePoint Server 2010, through the real stack.

SharePoint has no ranged read: its client returns the whole document for any
read. Before the generic fallback, `offset`/`length` on it answered "This
connection does not support windowed reads", and a document longer than the
whole-read observation budget was unreadable past its first few thousand
characters. Now the tool pages the text itself, in characters, under the same
window contract network_dir and S3 serve natively.

Only the SharePoint SOAP server is simulated; the connection, data source and
report are real rows and the tool resolves the client exactly as the agent
loop does.
"""
from __future__ import annotations

import asyncio
import io
import zipfile

import pytest

from app.dependencies import async_session_maker
from tests.e2e.test_sharepoint_2010_index_search import _ctx, _last, _seed
from tests.unit.test_sharepoint_2010_client import ROOT, legacy  # noqa: F401


def _long_text(n_lines: int) -> str:
    # Varied line lengths so windows end mid-line unless snapped; an id in
    # every line so a dropped or duplicated page is detectable.
    return "\n".join(
        f"L{i:05d} " + ("section header" if i % 50 == 0 else "body text " * (1 + i % 9))
        for i in range(n_lines)
    ) + "\n"


def _docx(text: str) -> bytes:
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{ln}</w:t></w:r></w:p>' for ln in text.split("\n"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", (
            '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'))
        z.writestr("_rels/.rels", (
            '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'))
        z.writestr("word/document.xml", (
            '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{body}</w:body></w:document>"))
    return buf.getvalue()


async def _flow(server, index_mode: str):
    from app.ai.tools.implementations.read_file import ReadFileTool

    text = _long_text(420)
    assert len(text) > 20_000
    server.blobs[ROOT + "/Finance/annual_report.txt"] = text.encode("utf-8")
    server.blobs[ROOT + "/Finance/strategy_memo.docx"] = _docx(text.rstrip("\n"))

    async with async_session_maker() as db:
        org_id, user_id, conn_id, _ds_id, report_id = await _seed(db, index_mode)
        ctx = await _ctx(db, org_id, user_id, report_id)

        async def read(**inputs):
            return await _last(ReadFileTool().run_stream({"connection_id": str(conn_id), **inputs}, ctx))

        for file_id, window in (("Finance/annual_report.txt", 8000), ("Finance/strategy_memo.docx", 6000)):
            whole = (await read(file_id=file_id, max_chars=500_000))["output"]
            assert whole["success"] and whole["content_type"] == "text" and not whole["truncated"]
            reference = whole["text"]
            assert len(reference) > 2 * window

            pages, offset = [], 0
            for _ in range(50):
                payload = await read(file_id=file_id, offset=offset, length=window)
                out = payload["output"]
                assert out["success"], out.get("error")
                assert out["windowed"] is True and out["unit"] == "chars" and out["encoding"] == "text"
                assert len(out["text"]) <= window
                # The model reads the observation: the window text must be there in full.
                assert out["text"] in payload["observation"]["details"]
                pages.append(out)
                if out["eof"]:
                    assert out["next_cursor"] is None
                    break
                assert out["next_cursor"] > offset
                offset = out["next_cursor"]
            else:
                pytest.fail(f"{file_id}: paging never reached eof")

            assert len(pages) >= 3, file_id
            assert "".join(p["text"] for p in pages) == reference, file_id
            assert all(p["total_size"] == len(reference) for p in pages)

            # Past the end: an empty eof window, never an error.
            out = (await read(file_id=file_id, offset=len(reference) + 10, length=window))["output"]
            assert out["success"] and out["text"] == "" and out["eof"] is True


@pytest.mark.e2e
@pytest.mark.parametrize("index_mode", ["content", "metadata"])
def test_sharepoint_2010_documents_page_with_offset_length(legacy, index_mode):  # noqa: F811
    asyncio.run(_flow(legacy, index_mode))
