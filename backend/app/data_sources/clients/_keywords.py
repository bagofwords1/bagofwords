"""Deterministic, language-agnostic keyword extraction for the file index.

Turns a file's extracted text (+ its filename) into a ranked keyword list that
gets stored on the catalog row so the agent can find files by topic without
re-parsing them. No LLM, no per-language setup — a Unicode word tokenizer plus a
small English/Hebrew stopword list, ranked by frequency. Works on the mixed
Hebrew + English corpora real directories contain.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import List, Optional

# Unicode "word" = 2+ letters (any script), no digits/punctuation. `\w` minus
# digits/underscore via a negated class keeps Hebrew, accented Latin, etc.
_WORD_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)
# Filename/path tokens additionally keep digit runs: an id-like name
# (`6044534/appendix.pdf`) is exactly what people search for. Bodies
# stay letters-only so numeric tables don't flood the keyword list.
_NAME_RE = re.compile(r"[^\W_]{2,}", re.UNICODE)

# Small, high-frequency stopword set. English + common Hebrew function words —
# deliberately short (recall over precision; distinctive terms survive anyway).
_STOPWORDS = {
    # English
    "the", "and", "for", "are", "but", "not", "you", "all", "any", "can", "her",
    "was", "one", "our", "out", "day", "get", "has", "him", "his", "how", "man",
    "new", "now", "old", "see", "two", "way", "who", "boy", "did", "its", "let",
    "put", "say", "she", "too", "use", "that", "with", "this", "from", "they",
    "will", "would", "there", "their", "what", "about", "which", "when", "your",
    "them", "than", "then", "into", "have", "more", "some", "such", "only",
    "other", "been", "were", "also", "shall", "may", "each", "per", "via",
    # Hebrew (common function words)
    "של", "את", "על", "עם", "כל", "או", "גם", "כי", "זה", "זו", "הוא", "היא",
    "אני", "אנחנו", "הם", "לא", "כן", "אם", "אבל", "כמו", "יש", "אין", "רק",
}


def extract_keywords(
    text: Optional[str], filename: Optional[str] = None, max_keywords: int = 50
) -> List[str]:
    """Return up to `max_keywords` ranked keywords for a file.

    Filename tokens are always included first (so a file is findable by the
    words in its name even if its body is empty/binary), then the most frequent
    non-stopword body tokens fill the remainder.
    """
    keywords: List[str] = []
    seen = set()

    def _add(tok: str):
        if tok and tok not in seen and tok not in _STOPWORDS:
            seen.add(tok)
            keywords.append(tok)

    # Filename tokens first — always searchable.
    for tok in _NAME_RE.findall((filename or "").lower()):
        _add(tok)
        if len(keywords) >= max_keywords:
            return keywords[:max_keywords]

    if text:
        counts = Counter(
            t for t in (m.lower() for m in _WORD_RE.findall(text))
            if t not in _STOPWORDS
        )
        for tok, _ in counts.most_common():
            _add(tok)
            if len(keywords) >= max_keywords:
                break

    return keywords[:max_keywords]


def _ranked_tokens(text: str) -> list[str]:
    counts = Counter(
        t for t in (m.lower() for m in _WORD_RE.findall(text or ""))
        if t not in _STOPWORDS
    )
    return [tok for tok, _ in counts.most_common()]


def extract_keywords_from_parts(
    parts: list[str], filename: str | None = None, max_keywords: int = 50
) -> list[str]:
    """Keywords for a file made of several documents (an email with
    attachments): filename tokens first, then the parts take turns — each
    part's next most frequent unseen token — so every part is represented
    instead of the longest one taking the whole budget."""
    keywords: list[str] = []
    seen = set()

    def _add(tok: str) -> None:
        if tok and tok not in seen and tok not in _STOPWORDS:
            seen.add(tok)
            keywords.append(tok)

    for tok in _NAME_RE.findall((filename or "").lower()):
        _add(tok)
        if len(keywords) >= max_keywords:
            return keywords[:max_keywords]

    queues = [iter(_ranked_tokens(p)) for p in parts if p]
    while queues and len(keywords) < max_keywords:
        alive = []
        for q in queues:
            for tok in q:
                if tok not in seen:
                    _add(tok)
                    alive.append(q)
                    break
            if len(keywords) >= max_keywords:
                break
        queues = alive
    return keywords[:max_keywords]


# An email is several documents in one file (body, attachments, forwarded
# messages), so it gets a larger share of keywords than a single document.
EMAIL_KEYWORD_FACTOR = 2


def extract_file_keywords(
    text: str | None, filename: str | None = None, max_keywords: int = 50
) -> list[str]:
    """Keywords for a catalog row: what the content indexers call.

    Emails (.eml / .msg) are indexed from their parts, without the rendering
    scaffolding the model reads (labels, sub-file ids, separators) — see
    ``_email_text.email_index_parts``. Every other file is unchanged."""
    from app.data_sources.clients._email_text import email_index_parts, is_email_name

    if text and is_email_name(filename or ""):
        return extract_keywords_from_parts(
            email_index_parts(text), filename, max_keywords * EMAIL_KEYWORD_FACTOR,
        )
    return extract_keywords(text, filename, max_keywords)
