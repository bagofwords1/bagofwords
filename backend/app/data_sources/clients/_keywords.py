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
    for tok in _WORD_RE.findall((filename or "").lower()):
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
