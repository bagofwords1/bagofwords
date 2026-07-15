"""Feedback loop — `GET /api/reports/{id}/completions` 500s with
`UnicodeEncodeError: 'utf-8' codec can't encode characters … surrogates not
allowed` after the agent read a PDF.

Root cause chain: pypdf's extract_text() emits lone UTF-16 surrogates
(U+D800–DFFF) for PDFs whose ToUnicode CMap maps glyphs into the surrogate
range. `extract_document_text` returns that text unsanitized; it flows into
read_file output and is persisted with the completion. Python strings hold
lone surrogates happily, so nothing fails until the API re-serves the row:
starlette's JSONResponse does `json.dumps(..., ensure_ascii=False)
.encode("utf-8")` — and UTF-8 forbids lone surrogates → 500 on every load of
that report.

The crafted PDF below makes REAL pypdf produce a lone surrogate — no mocks.
Invariant under test: text leaving the document-extraction chokepoint must be
UTF-8 encodable (JSON-serializable end to end).
"""
from __future__ import annotations

import io

import pytest

from app.data_sources.clients._document_text import extract_document_text
from app.data_sources.clients.network_dir_client import NetworkDirClient

# ToUnicode CMap: glyph <01> maps to a LONE SURROGATE (U+D800); glyph <02>
# maps to "A" so the extraction clears the MIN_USABLE_DOC_CHARS gate and flows
# through the app as text (not the raw-bytes vision fallback).
_CMAP = b"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<00> <FF>
endcodespacerange
2 beginbfchar
<01> <D800>
<02> <0041>
endbfchar
endcmap
CMapName currentdict /CMap defineresource pop
end
end
"""

_CONTENT = b"BT /F1 12 Tf 72 720 Td <0102020202020202020202020202020202020202> Tj ET"


def _obj(n: int, body: bytes) -> bytes:
    return f"{n} 0 obj\n".encode() + body + b"\nendobj\n"


def build_surrogate_pdf() -> bytes:
    """Minimal one-page PDF whose extracted text contains U+D800."""
    objects = [
        _obj(1, b"<< /Type /Catalog /Pages 2 0 R >>"),
        _obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
        _obj(3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"),
        _obj(4, b"<< /Length " + str(len(_CONTENT)).encode() + b" >>\nstream\n"
                + _CONTENT + b"\nendstream"),
        _obj(5, b"<< /Type /Font /Subtype /TrueType /BaseFont /Helvetica "
                b"/FirstChar 1 /LastChar 2 /Widths [500 500] /ToUnicode 6 0 R >>"),
        _obj(6, b"<< /Length " + str(len(_CMAP)).encode() + b" >>\nstream\n"
                + _CMAP + b"\nendstream"),
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for o in objects:
        offsets.append(out.tell())
        out.write(o)
    xref_at = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
              f"startxref\n{xref_at}\n%%EOF\n".encode())
    return out.getvalue()


def _has_lone_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(c) <= 0xDFFF for c in text or "")


def test_premise_pypdf_emits_lone_surrogates(tmp_path):
    """Sanity check of the premise (not the fix): real pypdf yields U+D800
    from this PDF. If this ever fails, pypdf started sanitizing and the
    chokepoint scrub is belt-and-braces."""
    pdf = tmp_path / "bad.pdf"
    pdf.write_bytes(build_surrogate_pdf())
    from pypdf import PdfReader
    raw = PdfReader(io.BytesIO(pdf.read_bytes())).pages[0].extract_text()
    assert _has_lone_surrogate(raw)


def test_extracted_document_text_is_utf8_encodable(tmp_path):
    pdf = tmp_path / "bad.pdf"
    pdf.write_bytes(build_surrogate_pdf())
    text = extract_document_text(str(pdf), "bad.pdf")
    assert text  # extraction succeeded and cleared the usability gate
    text.encode("utf-8")  # UnicodeEncodeError here = the reported 500


def build_multipage_pdf(page_texts) -> bytes:
    """Minimal N-page PDF with plain-ASCII text per page (standard font, no
    ToUnicode — pypdf extracts it via the built-in encoding)."""
    objects = []
    n_pages = len(page_texts)
    kids = " ".join(f"{3 + i * 2} 0 R" for i in range(n_pages))
    objects.append(_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>"))
    objects.append(_obj(2, f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode()))
    font_num = 3 + n_pages * 2
    for i, text in enumerate(page_texts):
        page_num = 3 + i * 2
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objects.append(_obj(
            page_num,
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {page_num + 1} 0 R /Resources << /Font << /F1 {font_num} 0 R >> >> >>".encode(),
        ))
        objects.append(_obj(
            page_num + 1,
            b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        ))
    objects.append(_obj(
        font_num, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ))
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for o in objects:
        offsets.append(out.tell())
        out.write(o)
    xref_at = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
              f"startxref\n{xref_at}\n%%EOF\n".encode())
    return out.getvalue()


class TestSanitizeJsonStrings:
    def test_scrubs_surrogates_and_nul_recursively(self):
        from app.utils.json_sanitize import sanitize_json_strings

        dirty = {"a": "ok", "b": ["x\ud800y", {"c": "n\x00ul"}], "n": 7}
        clean = sanitize_json_strings(dirty)
        import json as _json
        _json.dumps(clean, ensure_ascii=False).encode("utf-8")  # must not raise
        # Surrogate replaced by SOME placeholder; surrounding chars survive.
        assert clean["b"][0].startswith("x") and clean["b"][0].endswith("y")
        assert "\ud800" not in clean["b"][0]
        assert clean["b"][1]["c"] == "nul"
        assert clean["n"] == 7

    def test_clean_objects_pass_through_unchanged(self):
        from app.utils.json_sanitize import sanitize_json_strings

        obj = {"a": ["b", {"c": 1}]}
        assert sanitize_json_strings(obj) is obj


class TestPdfPageRange:
    PAGES = ["Alpha page UNIQUE-A", "Bravo page UNIQUE-B", "Charlie page UNIQUE-C"]

    def test_extract_pdf_pages_text(self, tmp_path):
        from app.data_sources.clients._document_text import extract_pdf_pages_text

        pdf = tmp_path / "book.pdf"
        pdf.write_bytes(build_multipage_pdf(self.PAGES))
        text, total = extract_pdf_pages_text(str(pdf), 2, 2)
        assert total == 3
        assert "UNIQUE-B" in text
        assert "UNIQUE-A" not in text and "UNIQUE-C" not in text

    def test_network_dir_page_range_read(self, tmp_path):
        pdf = tmp_path / "book.pdf"
        pdf.write_bytes(build_multipage_pdf(self.PAGES))
        client = NetworkDirClient(root_path=str(tmp_path))
        paged = client.read_file("book.pdf", page_range=(2, 3))
        assert paged["__doc_pages__"] is True
        assert paged["pages_total"] == 3
        assert "UNIQUE-B" in paged["text"] and "UNIQUE-C" in paged["text"]
        assert "UNIQUE-A" not in paged["text"]

    def test_page_range_rejected_for_non_pdf(self, tmp_path):
        (tmp_path / "notes.txt").write_text("hello")
        client = NetworkDirClient(root_path=str(tmp_path))
        with pytest.raises(ValueError):
            client.read_file("notes.txt", page_range=(1, 2))

    @pytest.mark.asyncio
    async def test_tool_page_range_flow(self, tmp_path):
        from unittest.mock import AsyncMock, patch
        from app.ai.tools.implementations.read_file import ReadFileTool

        pdf = tmp_path / "book.pdf"
        pdf.write_bytes(build_multipage_pdf(self.PAGES))
        client = NetworkDirClient(root_path=str(tmp_path))
        with patch(
            "app.ai.tools.implementations.read_file.resolve_file_client",
            new=AsyncMock(return_value=(client, None)),
        ):
            events = [e async for e in ReadFileTool().run_stream(
                {"connection_id": "C1", "file_id": "book.pdf", "page_range": "2"}, {},
            )]
        payload = events[-1].payload
        out = payload["output"]
        assert out["success"] is True
        assert out["pages_shown"] == "2-2" and out["pages_total"] == 3
        # The page content reaches the model via the observation.
        assert "UNIQUE-B" in (payload["observation"].get("details") or "")

    @pytest.mark.asyncio
    async def test_tool_rejects_malformed_page_range(self, tmp_path):
        from unittest.mock import AsyncMock, patch
        from app.ai.tools.implementations.read_file import ReadFileTool

        client = NetworkDirClient(root_path=str(tmp_path))
        with patch(
            "app.ai.tools.implementations.read_file.resolve_file_client",
            new=AsyncMock(return_value=(client, None)),
        ):
            events = [e async for e in ReadFileTool().run_stream(
                {"connection_id": "C1", "file_id": "x.pdf", "page_range": "5-2"}, {},
            )]
        assert events[-1].payload["output"]["success"] is False


def test_read_file_payload_survives_json_response(tmp_path):
    """The exact reported boundary: content read through the file client must
    survive starlette's JSONResponse render (json.dumps + utf-8 encode)."""
    pdf = tmp_path / "docs" / "bad.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(build_surrogate_pdf())

    client = NetworkDirClient(root_path=str(pdf.parent))
    payload = client.read_file("bad.pdf")
    assert isinstance(payload, str)  # extracted as text, not vision fallback

    from starlette.responses import JSONResponse
    # On main this raises UnicodeEncodeError: surrogates not allowed — the
    # same crash as GET /api/reports/{id}/completions on a poisoned row.
    JSONResponse({"completions": [{"result_json": {"text": payload}}]})
