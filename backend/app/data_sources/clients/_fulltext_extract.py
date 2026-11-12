"""Turn one file on disk into full-text sections (index tier ``fulltext``).

Shared by every file source that can put a file on local disk: a file share
reads in place, an object store downloads to a temporary file first. The
section locations are in ``read_file`` coordinates, so what the index points
at is what the agent can ask for:

    pdf                  → ``page``  (read_file page_range)
    xlsx / xls           → ``sheet`` (read_file sheet)
    docx / pptx / email  → ``chars`` of the extracted text (read_file offset)
    text / csv / logs    → ``bytes`` of the file (read_file offset)
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

import pandas as pd

from app.data_sources.clients._document_text import (
    DOC_EXTS,
    doc_text_looks_garbled,
    extract_document_text,
    extract_pdf_page_texts,
)
from app.data_sources.clients._fulltext import (
    FileSections,
    name_section,
    sections_from_byte_stream,
    sections_from_pages,
    sections_from_sheets,
    sections_from_text,
)
from app.data_sources.clients.progress import IndexingCancelled

TABULAR_TEXT_EXTS = {"csv", "tsv"}
TEXT_EXTS = {"txt", "md", "json", "html", "htm", "log", "yaml", "yml", "xml", "py", "sql", "ndjson", "jsonl"}
EXCEL_EXTS = {"xlsx", "xls"}
INDEXABLE_EXTS = TEXT_EXTS | TABULAR_TEXT_EXTS | EXCEL_EXTS | DOC_EXTS

# Caps per file. Past these the file is indexed up to the cap and recorded as
# `partial`, so the agent is told that a miss inside it is not proof.
FULLTEXT_MAX_TEXT_BYTES = 64 * 1024 * 1024   # plain text / csv / logs
FULLTEXT_MAX_DOC_CHARS = 20_000_000          # extracted pdf/docx/pptx/xlsx/email text
FULLTEXT_MAX_PDF_PAGES = 5000
# How much of a file's text feeds its catalog keywords — the same sample the
# keyword tier uses, so both tiers describe a file with the same keywords.
KEYWORD_SAMPLE_CHARS = 200_000


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if name and "." in name else ""


def file_sections_from_path(path: Path, name: str, *, max_file_bytes: Optional[int] = None,
                            ext: Optional[str] = None,
                            text_truncated: bool = False) -> Tuple[FileSections, str]:
    """Split the file at ``path`` into full-text sections.

    ``name`` is the file's path as the user knows it (indexed as section 0, so
    every file is findable by name). ``ext`` overrides the extension taken
    from ``path`` (a temporary download has its own name). ``text_truncated``
    marks a text file whose download was cut at the size cap.

    Returns (sections, sample) — ``sample`` is the first stretch of the text,
    for the catalog keywords, so the file is not read twice. Never raises for
    a bad file: an unreadable one comes back as ``error`` with just its name.
    """
    path = Path(path)
    doc = FileSections(sections=[name_section(name)])
    ext = (ext or _ext(name) or _ext(path.name)).lower()
    sample = ""
    try:
        size = path.stat().st_size
        if ext in TEXT_EXTS | TABULAR_TEXT_EXTS:
            with path.open("rb") as stream:
                sections, truncated, read = sections_from_byte_stream(
                    stream, max_bytes=FULLTEXT_MAX_TEXT_BYTES)
            doc.sections += sections
            doc.chars = read
            if truncated or text_truncated:
                doc.status = "partial"
            with path.open("rb") as stream:
                sample = stream.read(KEYWORD_SAMPLE_CHARS).decode("utf-8", errors="ignore")
        elif ext not in INDEXABLE_EXTS:
            doc.status = "no_text"
        elif max_file_bytes and size > max_file_bytes:
            # Over the connection's size limit: reads are refused too, so the
            # content is neither indexed nor readable.
            doc.status = "partial"
        elif ext == "pdf":
            pages, _total, truncated = extract_pdf_page_texts(
                str(path), max_pages=FULLTEXT_MAX_PDF_PAGES, max_chars=FULLTEXT_MAX_DOC_CHARS)
            sample = "\n".join(t for _, t in pages)[:KEYWORD_SAMPLE_CHARS]
            if doc_text_looks_garbled(sample):
                # Glyph soup (broken font map): indexing it would only poison
                # the index. Findable by name; readable as images.
                pages, sample = [], ""
            doc.sections += sections_from_pages(pages)
            doc.chars = sum(len(t) for _, t in pages)
            if truncated:
                doc.status = "partial"
        elif ext in DOC_EXTS:
            text = extract_document_text(str(path), name, max_chars=FULLTEXT_MAX_DOC_CHARS)
            if doc_text_looks_garbled(text):
                text = ""
            doc.sections += sections_from_text(text)
            doc.chars = len(text)
            sample = text[:KEYWORD_SAMPLE_CHARS]
            if len(text) >= FULLTEXT_MAX_DOC_CHARS:
                doc.status = "partial"
        elif ext in EXCEL_EXTS:
            frames = pd.read_excel(path, sheet_name=None, header=None)
            sheets = []
            used = 0
            for sheet_name, df in frames.items():
                body = df.to_csv(index=False, header=False)
                if used + len(body) > FULLTEXT_MAX_DOC_CHARS:
                    body = body[: max(0, FULLTEXT_MAX_DOC_CHARS - used)]
                    doc.status = "partial"
                used += len(body)
                sheets.append((str(sheet_name), body))
            doc.sections += sections_from_sheets(sheets)
            doc.chars = used
            sample = "\n".join(f"{n}\n{b}" for n, b in sheets)[:KEYWORD_SAMPLE_CHARS]
        if doc.status == "indexed" and len(doc.sections) <= 1:
            doc.status = "no_text"
    except IndexingCancelled:
        raise
    except Exception:
        doc = FileSections(sections=[name_section(name)], status="error")
        sample = ""
    return doc, sample
