"""Best-effort plain-text extraction for rich document formats.

Lets file sources make PDF / Word / PowerPoint content *searchable* and
*readable* (not just opaque bytes). Dependency-light: PDF via pypdf, PPTX via
python-pptx (both already project deps), DOCX by reading the OOXML zip directly
(no python-docx needed).

Every extractor is defensive — a corrupt/locked/oversized file or a missing
optional lib yields "" rather than raising, so search over a mixed directory
never dies on one bad file.
"""
from __future__ import annotations

import logging
import re
import zipfile
from typing import Optional

logger = logging.getLogger(__name__)

# Extensions this module can turn into text. Callers gate on this set.
DOC_EXTS = {"pdf", "docx", "pptx"}

# Default cap on extracted characters. Bounds both memory and how much of a big
# document we scan on every search. ~200k chars ≈ 40-50 pages of prose.
DEFAULT_MAX_CHARS = 200_000

# How many PDF pages to read before giving up (paired with the char cap).
_PDF_MAX_PAGES = 200


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if name and "." in name else ""


def extract_document_text(path: str, name: Optional[str] = None,
                          max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Return extracted text for a pdf/docx/pptx file, or "" if unsupported /
    unreadable. `name` overrides the extension source (defaults to `path`)."""
    ext = _ext(name or path)
    try:
        if ext == "pdf":
            return _pdf(path, max_chars)
        if ext == "docx":
            return _docx(path, max_chars)
        if ext == "pptx":
            return _pptx(path, max_chars)
    except Exception as e:  # never let one bad file break a search
        logger.warning("extract_document_text failed for %s: %s", path, e)
    return ""


def _pdf(path: str, max_chars: int) -> str:
    from pypdf import PdfReader

    reader = PdfReader(path)
    parts: list[str] = []
    total = 0
    for i, page in enumerate(reader.pages):
        if i >= _PDF_MAX_PAGES:
            break
        try:
            t = page.extract_text() or ""
        except Exception:
            continue
        parts.append(t)
        total += len(t)
        if total >= max_chars:
            break
    return "\n".join(parts)[:max_chars]


# Match the visible-text runs in an OOXML part: <w:t> (Word) / <a:t> (PowerPoint).
_OOXML_TEXT_RE = re.compile(r"<(?:w|a):t[^>]*>(.*?)</(?:w|a):t>", re.DOTALL)


def _unescape(s: str) -> str:
    return (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&apos;", "'"))


def _docx(path: str, max_chars: int) -> str:
    with zipfile.ZipFile(path) as z:
        # Body plus headers/footers so contract text in any part is searchable.
        names = [n for n in z.namelist()
                 if n == "word/document.xml"
                 or (n.startswith("word/") and (n.startswith("word/header")
                     or n.startswith("word/footer")))]
        out: list[str] = []
        total = 0
        for n in names:
            xml = z.read(n).decode("utf-8", "ignore")
            for m in _OOXML_TEXT_RE.findall(xml):
                txt = _unescape(m)
                out.append(txt)
                total += len(txt)
            if total >= max_chars:
                break
    return " ".join(out)[:max_chars]


def _pptx(path: str, max_chars: int) -> str:
    try:
        from pptx import Presentation
    except Exception:
        return _ooxml_zip_fallback(path, "ppt/slides/", max_chars)

    prs = Presentation(path)
    out: list[str] = []
    total = 0
    for slide in prs.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                t = shape.text_frame.text
                if t:
                    out.append(t)
                    total += len(t)
        if total >= max_chars:
            break
    return "\n".join(out)[:max_chars]


def _ooxml_zip_fallback(path: str, prefix: str, max_chars: int) -> str:
    """python-pptx unavailable — scrape <a:t> runs straight from the slide parts."""
    with zipfile.ZipFile(path) as z:
        out: list[str] = []
        total = 0
        for n in sorted(z.namelist()):
            if not (n.startswith(prefix) and n.endswith(".xml")):
                continue
            xml = z.read(n).decode("utf-8", "ignore")
            for m in _OOXML_TEXT_RE.findall(xml):
                txt = _unescape(m)
                out.append(txt)
                total += len(txt)
            if total >= max_chars:
                break
    return "\n".join(out)[:max_chars]
