"""Helpers for collapsing overlapping instruction suggestions.

Sequential edits to the same instruction (often from separate chat turns) create
separate pending builds that each fork from the same un-promoted main. A later
edit typically re-captures the earlier (still-pending) text, so its snapshot is a
*superset* of the earlier one — e.g. build A proposes "+lorem" and build B
proposes "+lorem +hello". Rendered together they duplicate the shared text
("Lorem ipsum Lorem ipsum").

These builds are not chained at the ``base_build_id`` level (both point at the
same main), so the structural supersede in the review routes can't catch them.
We instead detect the relationship by *content*: if one suggestion's text is the
other's text plus pure insertions, the smaller is an intermediate the larger
already covers, and only the larger (cumulative) suggestion should surface.
"""
import difflib
from typing import List


def covers(small: str, big: str) -> bool:
    """True if ``big`` equals ``small`` with only insertions added — i.e. every
    character of ``small`` is preserved in ``big`` (no deletions/replacements).

    That makes ``big`` a strict cumulative superset of ``small``, so a suggestion
    proposing ``big`` already contains everything ``small`` proposes."""
    small = small or ""
    big = big or ""
    if small == big:
        return False
    if not small:
        return True  # the empty proposal is contained in anything non-empty
    sm = difflib.SequenceMatcher(None, small, big, autojunk=False)
    for tag, *_ in sm.get_opcodes():
        if tag in ("delete", "replace"):
            return False
    return True


def superseded_by_containment(items: dict) -> set:
    """Given ``{candidate_id: (pending_text, base_text)}`` for the pending
    suggestions on ONE instruction, return the ids that are intermediate
    snapshots a later sibling already covers — so only the maximal (leaf)
    cumulative suggestions are left out.

    A candidate ``a`` is superseded by ``b`` only when:
      * ``a`` is itself a *purely additive* edit over its own base
        (``covers(base_a, a)``) — it only inserts, never deletes; and
      * ``b`` extends ``a`` by further insertions (``covers(a, b)``).

    Requiring ``a`` to be additive over its base is what keeps a deletion-only
    suggestion safe: it is never silently dropped just because some unrelated
    additive sibling's text happens to contain its (shorter) text."""
    ids: List[str] = list(items.keys())
    superseded = set()
    for a in ids:
        if a in superseded:
            continue
        a_text, a_base = items[a]
        # Only an additive-over-its-base suggestion can be a covered intermediate.
        if not covers(a_base or "", a_text or ""):
            continue
        for b in ids:
            if a == b or b in superseded:
                continue
            b_text, _b_base = items[b]
            if covers(a_text or "", b_text or ""):   # b ⊋ a → a is intermediate
                superseded.add(a)
                break
    return superseded
