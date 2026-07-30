"""Feedback loop — read_file on a glyph-remapped PDF returns garbled text
instead of escalating to the image/vision fallback.

Root cause chain: subset-font PDFs with a missing/broken ToUnicode CMap render
pixel-perfect (rendering only needs code→glyph) but extract as low-ASCII
symbol soup (extraction needs code→Unicode, which is absent). The old gate —
`doc_text_is_usable` — is a pure LENGTH check, so hundreds of soup characters
sailed through as a successful text read, the vision fallback (keyed on
content_type == "binary") never fired, and the model was told to trust the
garbage ("never re-issue an identical read").

Fix under test, three layers:
- `doc_text_looks_garbled`: shape check (alpha-ratio + multi-letter word runs)
  that separates glyph soup from prose in any script and from numeric tables.
- read_file tool: garbled doc text (or an explicit `as_images=true`) re-fetches
  the original bytes and escalates to the existing page-image vision path;
  without vision the text is kept but flagged `garbled` with a warning.
- indexing hygiene: garbled extractions don't feed keyword indexing/grep.
"""
from __future__ import annotations

import io

import pytest

from app.data_sources.clients._document_text import (
    doc_text_is_usable,
    doc_text_looks_garbled,
)
from app.data_sources.clients.network_dir_client import NetworkDirClient

# The shape of the real-world sample (Hebrew bank statement, cp-subset font):
# punctuation/digit soup, isolated letters, zero multi-letter words.
GARBLED_SAMPLE = (
    "! \" # $ % # % !     % &! ' !\n"
    "! + , ! ' , % *  - . / 0 # 1 2 3 / 4 5 4 5   6 7 7 4 # 1 $ % 8 5 7 9\n"
    "/ : ; < % / <    4 5 3 / =\n"
    "% , % * > 0 ? @ < 7 A 9 B C / D E C F . 4 7 . / G E > ! $ , ! ' , % * H\n"
    "? @ < 7 A @ / 2 / G   > 0\n"
    "J K + !  $ ! \" 1 ! !  ! ! ! !  % \" 1  % + \" # H J L M N D 9\n"
    "- 0 % \" 1 % , + \"  9 - 0 : ; C <\n"
    "$ * Q P % 1 0 $ +  + * Q % \" * 0 $ +\n"
)

ENGLISH_SAMPLE = (
    "The quarterly revenue report shows a 15% increase over Q3, driven mainly "
    "by the enterprise segment. Total bookings reached 4.2M with churn "
    "holding steady at 2.1%."
)

HEBREW_SAMPLE = (
    "דוח תנועות בחשבון עבור חודש מאי. יתרת פתיחה 18,757.80 שח. "
    "הכנסות מלקוחות 39,654.51 שח. הוצאות שכירות 1,733.00 שח בלבד."
)

# A legit numeric table: letter-sparse, but its header words are real
# multi-letter runs — must NOT be flagged.
NUMERIC_TABLE_SAMPLE = (
    "Date Amount Balance\n"
    "12/04 175.82 18757.80\n13/04 39654.51 58588.13\n14/04 1733.00 56855.13\n"
    "15/04 40010.41 16844.72\n16/04 1368.91 18312.93\n17/04 159.30 28312.93\n"
    "18/04 10000.00 28537.17\n19/04 340.24 24014.05\n20/04 676.88 31301.97\n"
)


class TestGarbleDetector:
    def test_flags_glyph_soup(self):
        assert doc_text_looks_garbled(GARBLED_SAMPLE) is True
        # …and the old length gate alone would have let it through:
        assert doc_text_is_usable(GARBLED_SAMPLE) is True

    def test_passes_english_prose(self):
        assert doc_text_looks_garbled(ENGLISH_SAMPLE) is False

    def test_passes_hebrew_prose(self):
        assert doc_text_looks_garbled(HEBREW_SAMPLE) is False

    def test_passes_numeric_table_with_headers(self):
        assert doc_text_looks_garbled(NUMERIC_TABLE_SAMPLE) is False

    def test_short_and_empty_defer_to_length_gate(self):
        assert doc_text_looks_garbled("") is False
        assert doc_text_looks_garbled(None) is False
        assert doc_text_looks_garbled("x1!") is False


# --------------------------------------------------------------------------
# Minimal hand-built PDFs (mirrors test_pdf_surrogate_sanitization helpers).
# The "garbled" one carries symbol-soup text in a standard font: extraction
# "succeeds" (length-wise) but yields soup — the unit-level stand-in for a
# subset font with no ToUnicode map. It still rasterizes fine with pypdfium2.


def _obj(n: int, body: bytes) -> bytes:
    return f"{n} 0 obj\n".encode() + body + b"\nendobj\n"


def build_pdf(page_texts) -> bytes:
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
    objects.append(_obj(font_num, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))
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


# PDF string syntax forbids unescaped parens — the soup avoids them.
_SOUP_LINE = "! + , ! ' , % * - . / 0 # 1 2 3 / 4 5 4 5 6 7 7 4 # 1 $ % 8 5 7 9 / : ; < % / <"
GARBLED_PDF_PAGES = [_SOUP_LINE, _SOUP_LINE, _SOUP_LINE]
CLEAN_PDF_PAGES = [
    "Quarterly revenue increased fifteen percent over the previous quarter",
    "Enterprise bookings reached record levels across all regions",
]


class TestIndexHygiene:
    def test_network_dir_file_text_drops_garbled_doc(self, tmp_path):
        (tmp_path / "garbled.pdf").write_bytes(build_pdf(GARBLED_PDF_PAGES))
        (tmp_path / "clean.pdf").write_bytes(build_pdf(CLEAN_PDF_PAGES))
        client = NetworkDirClient(root_path=str(tmp_path))
        assert client._file_text(tmp_path / "garbled.pdf") == ""
        assert "Quarterly revenue" in client._file_text(tmp_path / "clean.pdf")


_WORDML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body><w:p><w:r><w:t>Quarterly revenue reached 4.2 million dollars.</w:t></w:r></w:p>
<w:p><w:r><w:t>Churn held steady at 2.1 percent.</w:t></w:r></w:p></w:body></w:document>'''

_WORDML_TABLE = '''<?xml version="1.0"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
<w:tbl>
 <w:tr><w:tc><w:p><w:r><w:t>Date</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Amount</w:t></w:r></w:p></w:tc></w:tr>
 <w:tr><w:tc><w:p><w:r><w:t>12/04</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>39,654.51</w:t></w:r></w:p></w:tc></w:tr>
</w:tbl>
<w:p><w:r><w:t xml:space="preserve">Closing balance stays healthy.</w:t></w:r></w:p>
</w:body></w:document>'''


def _zipped_docx(xml: str) -> bytes:
    import zipfile as _zf
    buf = io.BytesIO()
    with _zf.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


class TestDocxExtraction:
    """DOCX variants that used to come back as raw XML (or nothing)."""

    def _extract(self, tmp_path, name, data):
        from app.data_sources.clients._document_text import extract_document_text
        p = tmp_path / name
        p.write_bytes(data)
        return extract_document_text(str(p), name)

    def test_table_docx_extracts_without_markup(self, tmp_path):
        """The old regex matched <w:tbl>/<w:tr>/<w:tc>/<w:tab> as text opens,
        so any docx containing a TABLE leaked raw XML into its extraction."""
        t = self._extract(tmp_path, "table.docx", _zipped_docx(_WORDML_TABLE))
        assert "<" not in t
        assert "39,654.51" in t and "Date" in t and "Closing balance" in t

    def test_flat_wordml_docx_extracts(self, tmp_path):
        """Flat OPC / Word 2003 XML saved with a .docx name isn't a zip; it
        used to extract as '' and dead-end as an unrenderable binary."""
        t = self._extract(tmp_path, "flat.docx", _WORDML.encode())
        assert "Quarterly revenue" in t and "<" not in t

    def test_nonstandard_prefix_docx_extracts(self, tmp_path):
        xml = _WORDML.replace("w:", "ns0:").replace("xmlns:w=", "xmlns:ns0=")
        t = self._extract(tmp_path, "odd.docx", _zipped_docx(xml))
        assert "Quarterly revenue" in t

    def test_non_wordml_flat_file_stays_empty(self, tmp_path):
        """A random XML file with a .docx name must NOT be misread as Word."""
        t = self._extract(tmp_path, "notword.docx", b"<?xml version='1.0'?><data><t>x</t></data>")
        assert t == ""


class _VisionModel:
    supports_vision = True


@pytest.fixture
def cache_in_tmp(tmp_path, monkeypatch):
    """Point the read cache at tmp so tests never touch uploads/filecache."""
    from app.ai.tools.implementations import _file_cache
    monkeypatch.setattr(_file_cache, "_CACHE_ROOT", tmp_path / "filecache")
    return _file_cache


async def _run_read(tool_input, runtime_ctx, client):
    from unittest.mock import AsyncMock, patch
    from app.ai.tools.implementations.read_file import ReadFileTool
    with patch(
        "app.ai.tools.implementations.read_file.resolve_file_client",
        new=AsyncMock(return_value=(client, None)),
    ):
        events = [e async for e in ReadFileTool().run_stream(tool_input, runtime_ctx)]
    return events[-1].payload


class TestToolEscalation:
    @pytest.mark.asyncio
    async def test_garbled_pdf_escalates_to_images_with_vision(self, tmp_path, cache_in_tmp):
        (tmp_path / "stmt.pdf").write_bytes(build_pdf(GARBLED_PDF_PAGES))
        client = NetworkDirClient(root_path=str(tmp_path))
        payload = await _run_read(
            {"connection_id": "C1", "file_id": "stmt.pdf"},
            {"model": _VisionModel()}, client,
        )
        out = payload["output"]
        assert out["success"] is True
        assert out["content_type"] == "images"
        assert out["image_count"] == 3 and out["pages_total"] == 3
        assert "garbled" in payload["observation"]["summary"]
        assert payload["observation"].get("images")  # pages reach the model

    @pytest.mark.asyncio
    async def test_garbled_pdf_without_vision_keeps_flagged_text(self, tmp_path, cache_in_tmp):
        (tmp_path / "stmt.pdf").write_bytes(build_pdf(GARBLED_PDF_PAGES))
        client = NetworkDirClient(root_path=str(tmp_path))
        payload = await _run_read({"connection_id": "C1", "file_id": "stmt.pdf"}, {}, client)
        out = payload["output"]
        assert out["success"] is True
        assert out["content_type"] == "text"
        assert out["garbled"] is True
        assert "garbled" in (payload["observation"].get("details") or "")

    @pytest.mark.asyncio
    async def test_clean_pdf_stays_text(self, tmp_path, cache_in_tmp):
        (tmp_path / "report.pdf").write_bytes(build_pdf(CLEAN_PDF_PAGES))
        client = NetworkDirClient(root_path=str(tmp_path))
        payload = await _run_read(
            {"connection_id": "C1", "file_id": "report.pdf"},
            {"model": _VisionModel()}, client,
        )
        out = payload["output"]
        assert out["content_type"] == "text"
        assert not out.get("garbled")
        assert "Quarterly revenue" in (payload["observation"].get("details") or "")

    @pytest.mark.asyncio
    async def test_as_images_forces_image_read_on_clean_pdf(self, tmp_path, cache_in_tmp):
        (tmp_path / "report.pdf").write_bytes(build_pdf(CLEAN_PDF_PAGES))
        client = NetworkDirClient(root_path=str(tmp_path))
        payload = await _run_read(
            {"connection_id": "C1", "file_id": "report.pdf", "as_images": True},
            {"model": _VisionModel()}, client,
        )
        out = payload["output"]
        assert out["content_type"] == "images"
        assert out["image_count"] == 2

    @pytest.mark.asyncio
    async def test_as_images_bypasses_poisoned_text_cache(self, tmp_path, cache_in_tmp):
        """A pre-escalation cache entry holding garbled 'text' must not be
        served back to an explicit as_images read."""
        (tmp_path / "stmt.pdf").write_bytes(build_pdf(GARBLED_PDF_PAGES))
        client = NetworkDirClient(root_path=str(tmp_path))
        version = await client.afile_version("stmt.pdf")
        cache_in_tmp.write(
            "C1", "stmt.pdf", version,
            rendered={"content_type": "text", "text": GARBLED_SAMPLE, "truncated": False},
        )
        payload = await _run_read(
            {"connection_id": "C1", "file_id": "stmt.pdf", "as_images": True},
            {"model": _VisionModel()}, client,
        )
        assert payload["output"]["content_type"] == "images"
        # …and the fresh image render replaced the poisoned entry.
        refreshed = cache_in_tmp.read("C1", "stmt.pdf", version)
        assert refreshed and refreshed["rendered"]["content_type"] == "images"

    @pytest.mark.asyncio
    async def test_page_range_garbled_escalates_to_images(self, tmp_path, cache_in_tmp):
        (tmp_path / "stmt.pdf").write_bytes(build_pdf(GARBLED_PDF_PAGES))
        client = NetworkDirClient(root_path=str(tmp_path))
        payload = await _run_read(
            {"connection_id": "C1", "file_id": "stmt.pdf", "page_range": "2"},
            {"model": _VisionModel()}, client,
        )
        out = payload["output"]
        assert out["content_type"] == "images"
        assert out["pages_shown"] == "2-2"
        assert "garbled" in payload["observation"]["summary"]
