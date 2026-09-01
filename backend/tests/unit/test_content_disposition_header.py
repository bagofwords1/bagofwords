"""A non-Latin filename must not 500 the file routes.

HTTP headers are latin-1. Interpolating a Hebrew / Cyrillic / CJK filename
straight into `Content-Disposition: inline; filename="…"` raises
UnicodeEncodeError inside the ASGI server, and the client gets a 500 instead
of the file. Hit for real by an <iframe> preview of a PDF named
"סקירת אלבומים ומכירות.pdf": the error pointed at position 18, which is exactly
where the filename starts in `inline; filename="`.
"""
from __future__ import annotations

import pytest

from app.routes.file import _content_disposition

# Names that broke, or that the transliteration fallback has to survive.
NON_LATIN = [
    "סקירת אלבומים ומכירות.pdf",   # the file from the bug report
    "отчёт.xlsx",
    "データ.csv",
    "报告.pdf",
    "café.txt",
    "no-extension-שלום",
]

ASCII = ["report.pdf", "Book 7.xlsx", "a.b.c.txt"]


@pytest.mark.parametrize("name", NON_LATIN + ASCII + ["", ".pdf", 'a"b.txt'])
@pytest.mark.parametrize("kind", ["inline", "attachment"])
def test_header_is_always_latin_1_encodable(kind, name):
    """The invariant the ASGI server enforces — violating it is a 500."""
    _content_disposition(kind, name).encode("latin-1")


@pytest.mark.parametrize("name", NON_LATIN + ASCII + ["", ".pdf", 'a"b.txt'])
def test_quoted_string_stays_well_formed(name):
    """Exactly one quoted-string. An unescaped quote inside the name would
    terminate it early and corrupt every parameter after it."""
    assert _content_disposition("inline", name).count('"') == 2


@pytest.mark.parametrize("name", NON_LATIN)
def test_real_name_survives_in_the_rfc_5987_form(name):
    """Transliteration is a fallback for old clients, not a rename — the true
    name still has to reach the browser."""
    from urllib.parse import quote

    assert f"filename*=UTF-8''{quote(name)}" in _content_disposition("inline", name)


@pytest.mark.parametrize("name,expected", [
    ("סקירת אלבומים ומכירות.pdf", 'filename="file.pdf"'),
    ("отчёт.xlsx", 'filename="file.xlsx"'),
    (".pdf", 'filename="file.pdf"'),
])
def test_fallback_keeps_a_usable_extension(name, expected):
    """Stripping non-ASCII from "סקירת….pdf" leaves ".pdf" — which browsers
    save as a hidden, extensionless file. A stem with nothing left must be
    replaced outright rather than shipped as a dotfile."""
    assert expected in _content_disposition("inline", name)


def test_ascii_names_are_left_alone():
    assert 'filename="report.pdf"' in _content_disposition("attachment", "report.pdf")


@pytest.mark.parametrize("name", [
    "reports/2024/annual.pdf",       # connector ids are often full paths
    "evil\r\nX-Injected: 1.pdf",     # CR/LF must never reach a header value
    "tab\there.csv",
])
def test_control_chars_and_separators_cannot_break_the_header(name):
    """CR/LF survive an ascii encode, and h11 rejects (or worse, splits) the
    header at send time — the exact 500 class this helper exists to prevent."""
    header = _content_disposition("attachment", name)
    header.encode("latin-1")
    assert "\r" not in header and "\n" not in header and "\t" not in header


def test_slashes_are_percent_encoded_in_the_ext_value():
    """"/" is not an attr-char in RFC 5987: strict parsers drop the whole
    filename* when it appears literally. quote()'s default safe="/" kept it."""
    header = _content_disposition("inline", "reports/2024/annual.pdf")
    assert "filename*=UTF-8''reports%2F2024%2Fannual.pdf" in header


def test_backslash_cannot_escape_the_closing_quote():
    header = _content_disposition("attachment", 'trail\\')
    assert header.count('"') == 2
    assert '\\"' not in header
