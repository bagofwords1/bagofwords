"""Lexical keyword extraction + coverage scoring shared by context builders.

Extracted from InstructionContextBuilder so the memory context builder and
search_memory score text exactly the way instruction matching does. Pure
functions, no DB.
"""
from __future__ import annotations

import re
from typing import Iterable, Set

STOPWORDS = frozenset({
    "the", "a", "an", "of", "and", "for", "to", "in", "by", "with", "on",
    "is", "are", "be", "this", "that", "it", "as", "at", "from", "or",
    "what", "how", "when", "where", "why", "which", "who", "can", "will",
    "should", "would", "could", "have", "has", "had", "do", "does", "did",
    "i", "you", "we", "they", "he", "she", "my", "your", "our", "their",
    "me", "us", "them", "all", "some", "any", "no", "not", "but", "if",
    "show", "get", "find", "give", "tell", "list", "display", "want", "need",
})

_ASCII_SPLIT = re.compile(r"[^a-z0-9]+")
# Unicode-aware split: letters/digits in any script are word characters, so
# Hebrew or Spanish prompts produce keywords too. Underscore splits words.
_UNICODE_SPLIT = re.compile(r"[\W_]+", re.UNICODE)


def extract_keywords(text: str, stopwords: Iterable[str] = STOPWORDS, *, unicode: bool = False) -> Set[str]:
    """Lowercased words (≥2 chars) minus stopwords.

    ``unicode=False`` keeps the historical ASCII-only split instructions have
    always used; ``unicode=True`` keeps non-Latin words.
    """
    splitter = _UNICODE_SPLIT if unicode else _ASCII_SPLIT
    stop = stopwords if isinstance(stopwords, (set, frozenset)) else set(stopwords)
    words = splitter.split((text or "").lower())
    return {w for w in words if w and len(w) >= 2 and w not in stop}


def stem(word: str) -> str:
    """Very light suffix stripper so morphological variants map to the same
    stem (revenues/revenue, churned/churn, cancelling/cancel, matches/match).

    Both query and document keywords go through this, so the only thing
    that matters is consistency — not linguistic correctness.
    """
    if len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    stemmed = word
    if word.endswith("es") and len(word) - 2 >= 3 and (
        word[-3] in "sxz" or word.endswith(("ches", "shes"))
    ):
        stemmed = word[:-2]          # matches -> match, boxes -> box
    elif word.endswith("s") and not word.endswith("ss") and len(word) - 1 >= 3:
        stemmed = word[:-1]          # revenues -> revenue, sales -> sale
    else:
        for suffix in ("ing", "ed"):
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                stemmed = word[: -len(suffix)]
                break
    # Collapse a trailing double consonant (cancell -> cancel, plann -> plan)
    if len(stemmed) >= 4 and stemmed[-1] == stemmed[-2] and stemmed[-1] not in "aeiou":
        stemmed = stemmed[:-1]
    return stemmed


def score_text(searchable: str, keywords: Set[str], stopwords: Iterable[str] = STOPWORDS, *, unicode: bool = False) -> float:
    """Query-keyword coverage of ``searchable``: the fraction of ``keywords``
    found in the text (exactly, stem-equal, or as a substring in either
    direction). 0..1. Long texts are not penalized — only unmatched *query*
    words lower the score.
    """
    if not keywords:
        return 0.0
    searchable_lower = (searchable or "").lower()
    searchable_keywords = extract_keywords(searchable, stopwords, unicode=unicode)
    if not searchable_keywords and not searchable_lower.strip():
        return 0.0

    stemmed_searchable = {stem(w) for w in searchable_keywords}

    matched = 0.0
    for kw in keywords:
        if kw in searchable_keywords:
            matched += 1.0
            continue
        if stem(kw) in stemmed_searchable:
            matched += 0.9
            continue
        # Substring in the raw text (helps joined words: "invoiceline")
        if len(kw) >= 3 and kw in searchable_lower:
            matched += 0.8
            continue
        # Symmetric containment between keywords ("churn" ~ "churned",
        # "cancellation" query vs "cancel" in text)
        if len(kw) >= 4 and any(
            len(sk) >= 4 and (kw in sk or sk in kw) for sk in searchable_keywords
        ):
            matched += 0.7
    return matched / len(keywords)


def matched_keywords(searchable: str, keywords: Set[str], *, unicode: bool = False) -> Set[str]:
    """The subset of ``keywords`` that hit ``searchable`` (exact or stem-equal).
    Used to explain a match ("← matched: region")."""
    words = extract_keywords(searchable, unicode=unicode)
    stems = {stem(w) for w in words}
    return {kw for kw in keywords if kw in words or stem(kw) in stems}
