"""Full-text indexing primitives for file sources (index tier ``fulltext``).

Pure functions, no DB and no I/O beyond the file handed in:

- ``tokenize`` turns text into positioned terms. One tokenizer feeds both
  databases (Postgres ``tsvector`` / SQLite FTS5), so a query matches the same
  files on either. Numbers are kept (IDs are what people search for), English
  gets a light stem, and a Hebrew word also carries its form without the
  one-letter particles (ב/ה/ו/כ/ל/מ/ש) so "חוזה" finds "בחוזה".
- ``parse_query`` applies the same normalization to a search query.
- ``FileSections`` + the ``sections_from_*`` helpers split a file into
  sections whose location is expressed in ``read_file`` coordinates (PDF page,
  sheet name, character or byte offset), so a hit tells the agent exactly what
  to read.

Nothing here stores document text: the caller persists only the terms.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Bump when tokenization or sectioning changes in a way that makes stored
# sections disagree with fresh ones: files indexed under an older version are
# re-indexed on the next run.
FULLTEXT_INDEX_VERSION = 1

# Target section size (characters of extracted text). Small enough that a hit
# points at one screenful, large enough that ranking has context to work with.
SECTION_CHARS = 3000
# A single unit (a PDF page, a sheet) larger than this is split further.
SECTION_MAX_CHARS = 4500
# Positions past this are clamped by Postgres tsvector; sections stay far below.
_MAX_POSITION = 16000
_MAX_TOKEN_LEN = 64

# Letters/digits of any script; underscore and punctuation split words.
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
# Hebrew points/cantillation: stripped so pointed and unpointed text match.
_NIQQUD_RE = re.compile(r"[֑-ׇ]")
_HEBREW_RE = re.compile(r"[א-ת]")
# One-letter particles written attached to the next word.
_HEBREW_PREFIXES = "והבכלמש"
_HEBREW_MIN_STEM = 3

# Query-side only: words too common to drive a search. The index keeps every
# word (phrases need them); a query drops these unless nothing else is left.
QUERY_STOPWORDS = frozenset({
    "the", "and", "for", "are", "but", "not", "you", "all", "any", "can",
    "was", "one", "our", "out", "has", "his", "her", "how", "its", "who",
    "did", "that", "with", "this", "from", "they", "will", "would", "there",
    "their", "what", "about", "which", "when", "your", "them", "than", "then",
    "into", "have", "more", "some", "such", "only", "other", "been", "were",
    "also", "shall", "may", "each", "per", "via", "of", "to", "in", "is", "it",
    "on", "as", "at", "by", "be", "or", "an", "we", "do", "does", "if", "so",
    "where", "why", "find", "show", "get", "give", "tell", "list", "file",
    "files", "document", "documents", "mention", "mentions", "mentioned",
    "contain", "contains", "containing", "say", "says",
    "של", "את", "על", "עם", "כל", "או", "גם", "כי", "זה", "זו", "הוא", "היא",
    "אני", "אנחנו", "הם", "לא", "כן", "אם", "אבל", "כמו", "יש", "אין", "רק",
    "מה", "איפה", "איזה", "קובץ", "מסמך",
})


def _english_stem(word: str) -> str:
    """Light suffix stripper: plurals, -ing, -ed. Applied to documents and
    queries alike, so only consistency matters, not linguistic correctness.

    Same rules as ``app.ai.context.keyword_match.stem`` (instruction/memory
    matching); kept here so a file client does not import the agent context
    package. Changing either changes what matches — bump
    ``FULLTEXT_INDEX_VERSION`` when this one changes.
    """
    if len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    stemmed = word
    if word.endswith("es") and len(word) - 2 >= 3 and (
        word[-3] in "sxz" or word.endswith(("ches", "shes"))
    ):
        stemmed = word[:-2]
    elif word.endswith("s") and not word.endswith("ss") and len(word) - 1 >= 3:
        stemmed = word[:-1]
    else:
        for suffix in ("ing", "ed"):
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                stemmed = word[: -len(suffix)]
                break
    if len(stemmed) >= 4 and stemmed[-1] == stemmed[-2] and stemmed[-1] not in "aeiou":
        stemmed = stemmed[:-1]
    return stemmed


@dataclass(frozen=True)
class Term:
    """One word of a section: its position, its indexed form, and any
    alternative forms stored at the same position."""
    pos: int
    base: str
    alts: Tuple[str, ...] = ()


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    return _NIQQUD_RE.sub("", text).lower()


def hebrew_variants(word: str) -> Tuple[str, ...]:
    """Forms of a Hebrew word with up to two leading particles removed.

    Only forms with at least ``_HEBREW_MIN_STEM`` letters left are produced,
    so short native words ("בית") are left alone."""
    out: List[str] = []
    cur = word
    for _ in range(2):
        if len(cur) - 1 >= _HEBREW_MIN_STEM and cur[0] in _HEBREW_PREFIXES:
            cur = cur[1:]
            out.append(cur)
        else:
            break
    return tuple(out)


# Documents repeat their words: remembering the forms of each distinct word
# takes most of the cost out of tokenizing a large share. Bounded; cleared
# when full (IDs and numbers are the long tail and are cheap to recompute).
_FORMS_CACHE: Dict[str, Tuple[str, Tuple[str, ...]]] = {}
_FORMS_CACHE_MAX = 200_000


def _forms(raw: str) -> Tuple[str, Tuple[str, ...]]:
    """(indexed form, alternative forms) of one normalized word."""
    hit = _FORMS_CACHE.get(raw)
    if hit is not None:
        return hit
    word = raw[:_MAX_TOKEN_LEN]
    if _HEBREW_RE.search(word):
        out = (word, hebrew_variants(word))
    elif word.isascii() and word.isalpha():
        out = (_english_stem(word), ())
    else:
        out = (word, ())
    if len(_FORMS_CACHE) >= _FORMS_CACHE_MAX:
        _FORMS_CACHE.clear()
    _FORMS_CACHE[raw] = out
    return out


def tokenize(text: str) -> List[Term]:
    """Positioned terms of ``text``. Single characters are dropped; every
    other word — including numbers — is kept."""
    out: List[Term] = []
    pos = 0
    for m in _WORD_RE.finditer(_normalize(text)):
        raw = m.group(0)
        if len(raw) < 2:
            continue
        pos += 1
        base, alts = _forms(raw)
        out.append(Term(pos=min(pos, _MAX_POSITION), base=base, alts=alts))
    return out


# ------------------------------------------------------------------ queries


@dataclass
class QueryTerm:
    """One query word and every stored form that should count as a match."""
    forms: Tuple[str, ...]


@dataclass
class ParsedQuery:
    # Words: any of them may match; rarer ones count more.
    terms: List[QueryTerm] = field(default_factory=list)
    # Required phrases — a list of indexed forms that must appear in a row, in
    # this order. Quoted text and ID-like tokens ("PO-48213"): the user asked
    # for exactly this, so a file without it is not a result.
    phrases: List[List[str]] = field(default_factory=list)
    # Phrases that only raise the score when found ("cost/benefit").
    soft_phrases: List[List[str]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.terms and not self.phrases


_QUOTED_RE = re.compile(r'"([^"]+)"|“([^”]+)”|״([^״]+)״')
# The whole query is ranked on at most this many words: an agent may paste a
# sentence, and every extra word is another index lookup.
MAX_QUERY_TERMS = 12


def _query_forms(raw: str) -> Tuple[str, ...]:
    base, alts = _forms(raw[:_MAX_TOKEN_LEN])
    forms = [base, *alts]
    return tuple(dict.fromkeys(forms))


def parse_query(query: str) -> ParsedQuery:
    """Normalize a search query the way documents were normalized.

    Quoted text is a required phrase. A token written with joiners is indexed
    as its parts, so it is searched as a phrase of them: required when it
    looks like an identifier (has a digit: "PO-48213", "2024-11-05"),
    otherwise only a ranking boost ("cost/benefit"). Stopwords are dropped
    unless the query has nothing else.
    """
    parsed = ParsedQuery()
    text = query or ""
    for m in _QUOTED_RE.finditer(text):
        body = next(g for g in m.groups() if g)
        words = [t.base for t in tokenize(body)]
        if len(words) >= 2:
            parsed.phrases.append(words)
    # Joined tokens: split on whitespace first, look for inner punctuation.
    for chunk in re.split(r"\s+", _QUOTED_RE.sub(" ", text)):
        parts = [t.base for t in tokenize(chunk)]
        if len(parts) >= 2 and re.search(r"[^\W_][\-_./:#][^\W_]", _normalize(chunk)):
            if any(ch.isdigit() for ch in chunk):
                parsed.phrases.append(parts)
            else:
                parsed.soft_phrases.append(parts)

    seen: set = set()
    all_terms: List[QueryTerm] = []
    kept: List[QueryTerm] = []
    for m in _WORD_RE.finditer(_normalize(text)):
        raw = m.group(0)
        if len(raw) < 2:
            continue
        forms = _query_forms(raw)
        if forms[0] in seen:
            continue
        seen.add(forms[0])
        qt = QueryTerm(forms=forms)
        all_terms.append(qt)
        if raw not in QUERY_STOPWORDS:
            kept.append(qt)
    parsed.terms = (kept or all_terms)[:MAX_QUERY_TERMS]
    return parsed


# ----------------------------------------------------------------- sections


@dataclass
class Section:
    """A searchable slice of a file.

    ``kind``/``loc`` say where it is, in the terms ``read_file`` accepts:
    ``name`` (the file's own path — no content), ``page`` ({"page": n}),
    ``sheet`` ({"sheet": name}), ``chars`` / ``bytes`` ({"offset", "length"}).
    """
    kind: str
    loc: Dict[str, Any]
    terms: List[Term]


@dataclass
class FileSections:
    sections: List[Section] = field(default_factory=list)
    # indexed | partial (hit a size cap) | no_text (nothing extractable)
    status: str = "indexed"
    chars: int = 0


def name_section(path: str) -> Section:
    """Section 0 of every file: the words of its path, so a file is findable
    by name even when it has no extractable text."""
    return Section(kind="name", loc={}, terms=tokenize(path))


def _split_text(text: str, target: int = SECTION_CHARS) -> List[Tuple[int, str]]:
    """Split ``text`` into (start offset, slice) pieces of about ``target``
    characters, breaking at a newline or space when one is near the cut."""
    out: List[Tuple[int, str]] = []
    n = len(text)
    start = 0
    while start < n:
        end = min(n, start + target)
        if end < n:
            window = text.rfind("\n", start + target // 2, end)
            if window == -1:
                window = text.rfind(" ", start + target // 2, end)
            if window != -1:
                end = window + 1
        out.append((start, text[start:end]))
        start = end
    return out


def sections_from_text(text: str, *, unit: str = "chars") -> List[Section]:
    """Sections of one extracted text, located by character offset."""
    out: List[Section] = []
    for start, piece in _split_text(text or ""):
        terms = tokenize(piece)
        if terms:
            out.append(Section(kind=unit, loc={"offset": start, "length": len(piece)}, terms=terms))
    return out


def sections_from_pages(pages: Iterable[Tuple[int, str]]) -> List[Section]:
    """Sections of a paged document: one per page (1-based), a long page split
    into several sections that all point at the same page."""
    out: List[Section] = []
    for page_no, text in pages:
        if not text or not text.strip():
            continue
        pieces = [(0, text)] if len(text) <= SECTION_MAX_CHARS else _split_text(text)
        for _, piece in pieces:
            terms = tokenize(piece)
            if terms:
                out.append(Section(kind="page", loc={"page": page_no}, terms=terms))
    return out


def sections_from_sheets(sheets: Iterable[Tuple[str, str]]) -> List[Section]:
    """Sections of a workbook: the sheet name is indexed with its cells, and a
    large sheet becomes several sections pointing at the same sheet."""
    out: List[Section] = []
    for name, text in sheets:
        body = f"{name}\n{text or ''}"
        for _, piece in _split_text(body):
            terms = tokenize(piece)
            if terms:
                out.append(Section(kind="sheet", loc={"sheet": str(name)}, terms=terms))
    return out


def sections_from_byte_stream(stream, *, max_bytes: Optional[int] = None,
                              target: int = SECTION_CHARS) -> Tuple[List[Section], bool, int]:
    """Sections of a plain-text file read as a stream, located by BYTE offset
    (the unit ``read_file`` windows use for text files on a file share / S3).

    Breaks on line ends, so a section never starts mid-line; a line longer
    than the target is cut at the target. Returns (sections, truncated,
    bytes read).
    """
    out: List[Section] = []
    offset = 0
    buf = bytearray()
    buf_start = 0
    truncated = False

    def flush() -> None:
        nonlocal buf, buf_start
        if buf:
            terms = tokenize(bytes(buf).decode("utf-8", errors="ignore"))
            if terms:
                out.append(Section(kind="bytes", loc={"offset": buf_start, "length": len(buf)}, terms=terms))
        buf_start += len(buf)
        buf = bytearray()

    while True:
        if max_bytes is not None and offset >= max_bytes:
            truncated = True
            break
        line = stream.readline(target * 2)
        if not line:
            break
        if max_bytes is not None and offset + len(line) > max_bytes:
            line = line[: max_bytes - offset]
            truncated = True
        offset += len(line)
        buf.extend(line)
        if len(buf) >= target:
            flush()
        if truncated:
            break
    flush()
    return out, truncated, offset


def describe_location(kind: str, loc: Dict[str, Any]) -> str:
    """Short, model-facing description of a section's location that names the
    ``read_file`` argument to use."""
    if kind == "page":
        return f"page {loc.get('page')} (read_file page_range='{loc.get('page')}')"
    if kind == "sheet":
        return f"sheet '{loc.get('sheet')}' (read_file sheet='{loc.get('sheet')}')"
    if kind in ("chars", "bytes"):
        return (f"{kind} {loc.get('offset')}–{int(loc.get('offset') or 0) + int(loc.get('length') or 0)} "
                f"(read_file offset={loc.get('offset')} length={loc.get('length')})")
    return "file name"
