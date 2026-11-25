"""Extra conditions on a pattern rule's matches: a checksum, context words.

A rule's regex finds candidates; a :class:`FilteredPattern` keeps only those
that also pass the rule's ``checksum`` (e.g. Luhn for card numbers) and, when
``context`` words are set, have one of them nearby. It has the slice of the
``re.Pattern`` API the engine uses (``finditer``, ``findall``, ``sub``), so
every path — block detection, replacement, merged policies, display masking —
applies the same conditions.

Exact-term lists (``terms``) need no filter: they compile to a whole-word,
case-insensitive alternation (:func:`terms_pattern`).
"""

from __future__ import annotations

import re
from typing import Callable, Dict, Iterator, List, Optional, Sequence

# How far from a match (in characters, each side) a context word may be.
CONTEXT_WINDOW = 60

MAX_TERMS = 500
MAX_TERM_CHARS = 200
MAX_CONTEXT_WORDS = 50


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def luhn(value: str) -> bool:
    """Card numbers, IMEI, Canadian SIN…: the Luhn mod-10 check."""
    digits = _digits(value)
    if len(digits) < 2:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def iban(value: str) -> bool:
    """IBAN mod-97 (ISO 13616)."""
    s = "".join(ch for ch in value if ch.isalnum()).upper()
    if not 15 <= len(s) <= 34 or not s[:2].isalpha() or not s[2:4].isdigit():
        return False
    rearranged = s[4:] + s[:4]
    try:
        number = "".join(str(int(ch, 36)) for ch in rearranged)
    except ValueError:
        return False
    return int(number) % 97 == 1


def il_id(value: str) -> bool:
    """Israeli ID (Teudat Zehut): up to 9 digits, left-padded, weights 1,2."""
    digits = _digits(value)
    if not 5 <= len(digits) <= 9 or int(digits) == 0:
        return False
    digits = digits.zfill(9)
    total = 0
    for i, ch in enumerate(digits):
        d = int(ch) * (1 if i % 2 == 0 else 2)
        total += d - 9 if d > 9 else d
    return total % 10 == 0


def aba(value: str) -> bool:
    """US ABA routing number: 9 digits, weights 3-7-1."""
    digits = _digits(value)
    if len(digits) != 9:
        return False
    w = (3, 7, 1) * 3
    return sum(int(d) * k for d, k in zip(digits, w)) % 10 == 0


VALIDATORS: Dict[str, Callable[[str], bool]] = {
    "luhn": luhn,
    "iban": iban,
    "il_id": il_id,
    "aba": aba,
}


def terms_pattern(terms: Sequence[str]) -> Optional[str]:
    """One regex matching any of ``terms`` as a whole word or phrase
    (case-insensitive: the engine compiles with IGNORECASE). Longest first, so
    "Acme Corp" wins over "Acme"."""
    cleaned = sorted({t.strip() for t in terms if isinstance(t, str) and t.strip()}, key=lambda t: (-len(t), t))
    if not cleaned:
        return None
    body = "|".join(re.escape(t) for t in cleaned)
    return rf"(?<!\w)(?:{body})(?!\w)"


def context_regex(words: Sequence[str]) -> Optional[re.Pattern]:
    cleaned = sorted({w.strip() for w in words if isinstance(w, str) and w.strip()}, key=lambda w: (-len(w), w))
    if not cleaned:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(w) for w in cleaned) + r")(?!\w)", re.IGNORECASE)


class FilteredPattern:
    """A compiled regex whose matches must also pass a validator and/or have a
    context word within :data:`CONTEXT_WINDOW` characters."""

    def __init__(self, pattern: re.Pattern, *, validator: Optional[str] = None, context: Optional[re.Pattern] = None):
        self.pattern = pattern
        self.validator = VALIDATORS.get(validator) if validator else None
        self.context = context

    def accepts(self, m: re.Match) -> bool:
        if self.validator is not None and not self.validator(m.group(0)):
            return False
        if self.context is not None:
            text = m.string
            lo = max(0, m.start() - CONTEXT_WINDOW)
            hi = min(len(text), m.end() + CONTEXT_WINDOW)
            if not (self.context.search(text, lo, m.start()) or self.context.search(text, m.end(), hi)):
                return False
        return True

    def finditer(self, text: str) -> Iterator[re.Match]:
        return (m for m in self.pattern.finditer(text) if self.accepts(m))

    def findall(self, text: str) -> List[str]:
        return [m.group(0) for m in self.finditer(text)]

    def sub(self, repl, text: str) -> str:
        def _repl(m: re.Match) -> str:
            if not self.accepts(m):
                return m.group(0)
            return repl(m) if callable(repl) else m.expand(repl)
        return self.pattern.sub(_repl, text)


def wrap(patterns: List[re.Pattern], *, validator: Optional[str], context: Sequence[str]) -> list:
    """``patterns`` as they are when the rule has no extra condition, else
    each wrapped in a :class:`FilteredPattern`."""
    ctx = context_regex(context or [])
    if not (validator in VALIDATORS) and ctx is None:
        return list(patterns)
    return [FilteredPattern(p, validator=validator if validator in VALIDATORS else None, context=ctx) for p in patterns]
