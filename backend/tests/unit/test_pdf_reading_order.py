"""PDF text must come out in READING order, not in content-stream order.

pypdf's extract_text() concatenates text runs in the order the content stream
paints them. For Latin documents that is usually the reading order too, so the
difference was invisible for years. For Hebrew and Arabic it is not: the
extractor returned every line with its words reversed —

    pypdf   ".1ומכירותנח  אלבומים  סקירת"
    correct ".1 סקירת אלבומים ומכירות"

— and nothing downstream could tell. Every character is valid, correctly
encoded Hebrew, so doc_text_looks_garbled (which hunts for mojibake) never
fires and the model is handed fluent-looking gibberish as a faithful read. The
user sees confidently wrong answers about their own document.

Extraction therefore goes through PDFium, the engine Chromium renders PDFs
with, which reconstructs reading order from glyph positions.
"""
from __future__ import annotations

import io

import pytest

from app.data_sources.clients._document_text import (
    extract_document_text,
    extract_pdf_pages_text,
)


def _pdf_with_runs(runs) -> bytes:
    """A one-page PDF whose text runs are PAINTED in a different order than
    they READ. `runs` is [(x, text)]; they are emitted in list order but
    positioned by x, so stream order and reading order disagree — the same
    divergence that reverses RTL words."""
    body = "\n".join(
        f"BT /F1 14 Tf {x} 700 Td ({text}) Tj ET" for x, text in runs
    ).encode("ascii")

    def obj(n, payload):
        return f"{n} 0 obj\n".encode() + payload + b"\nendobj\n"

    objs = [
        obj(1, b"<< /Type /Catalog /Pages 2 0 R >>"),
        obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
        obj(3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
               b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"),
        obj(4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"),
        obj(5, f"<< /Length {len(body)} >>\nstream\n".encode() + body + b"\nendstream"),
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for o in objs:
        offsets.append(out.tell())
        out.write(o)
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
              f"startxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


# Painted right-to-left, so a stream-order extractor yields GAMMA…ALPHA…BETA.
SCRAMBLED_RUNS = [(300, "GAMMA"), (50, "ALPHA"), (175, "BETA")]


@pytest.fixture
def scrambled_pdf(tmp_path):
    p = tmp_path / "reading-order.pdf"
    p.write_bytes(_pdf_with_runs(SCRAMBLED_RUNS))
    return str(p)


def test_whole_document_extraction_follows_reading_order(scrambled_pdf):
    text = extract_document_text(scrambled_pdf, "reading-order.pdf")
    assert text.index("ALPHA") < text.index("BETA") < text.index("GAMMA"), (
        f"runs came back in paint order, not reading order: {text!r}"
    )


def test_page_range_extraction_follows_reading_order(scrambled_pdf):
    """The page_range path is a separate extractor and regressed independently
    — a Hebrew read scrambled the same way whether or not pages were named."""
    text, pages_total = extract_pdf_pages_text(scrambled_pdf, 1, 1)
    assert pages_total == 1
    assert text.index("ALPHA") < text.index("BETA") < text.index("GAMMA"), (
        f"runs came back in paint order, not reading order: {text!r}"
    )


def test_extraction_still_reads_plain_ltr_documents(tmp_path):
    """The switch must not disturb the ordinary case."""
    p = tmp_path / "plain.pdf"
    p.write_bytes(_pdf_with_runs([(50, "Revenue"), (150, "grew"), (250, "sharply")]))
    text = extract_document_text(str(p), "plain.pdf")
    for word in ("Revenue", "grew", "sharply"):
        assert word in text


def test_page_range_raises_on_an_unreadable_pdf(tmp_path):
    """extract_pdf_pages_text promises a real error rather than "" — a silent
    empty read strands the model re-reading the same pages forever."""
    p = tmp_path / "broken.pdf"
    p.write_bytes(b"not a pdf at all")
    with pytest.raises(Exception):
        extract_pdf_pages_text(str(p), 1, 1)


def test_whole_document_extraction_is_silent_on_an_unreadable_pdf(tmp_path):
    """The search-oriented path skips bad files instead of failing a crawl."""
    p = tmp_path / "broken.pdf"
    p.write_bytes(b"not a pdf at all")
    assert extract_document_text(str(p), "broken.pdf") == ""
