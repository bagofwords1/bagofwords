"""Which non-Latin script a short chat message is written in.

Used by the planner's per-turn reply-language hint (``PromptBuilderV3.
_format_reply_language``, mode ``detected``). Only scripts that identify one
language well enough are named; Latin text is never named, because a script
cannot tell English from Spanish.
"""
from __future__ import annotations

from typing import Optional

# (code, first code point, last code point) of each named Unicode block.
_SCRIPTS = (
    ("he", 0x0590, 0x05FF),
    ("ar", 0x0600, 0x06FF),
    ("ru", 0x0400, 0x04FF),
)
_MIN_SHARE = 0.6
_MIN_LETTERS = 2


def detect_message_script(text: Optional[str]) -> Optional[str]:
    """Return ``"he"``, ``"ar"`` or ``"ru"`` when that script holds at least
    60% of the message's letters and at least 2 of them; otherwise None.

    Digits, punctuation and combining marks are not letters. Every other
    letter (Latin included) counts toward the total only.
    """
    counts = {code: 0 for code, _, _ in _SCRIPTS}
    total = 0
    for ch in text or "":
        if not ch.isalpha():
            continue
        total += 1
        cp = ord(ch)
        for code, lo, hi in _SCRIPTS:
            if lo <= cp <= hi:
                counts[code] += 1
                break
    for code, n in counts.items():
        if n >= _MIN_LETTERS and n >= _MIN_SHARE * total:
            return code
    return None
