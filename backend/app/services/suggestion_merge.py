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
from typing import List


def covers(small: str, big: str) -> bool:
    """True if ``big`` equals ``small`` with only insertions added — i.e. every
    character of ``small`` is preserved in ``big`` (no deletions/replacements).

    That makes ``big`` a strict cumulative superset of ``small``, so a suggestion
    proposing ``big`` already contains everything ``small`` proposes.

    "Preserved, in order, with only insertions between" is precisely "``small``
    is a subsequence of ``big``", which one left-to-right scan decides in
    O(len(small) + len(big)).

    This used to ask ``difflib.SequenceMatcher(None, small, big, autojunk=False)``
    for a full character-level alignment and then discard everything except
    whether any ``delete``/``replace`` opcode appeared. That is quadratic, and
    ``autojunk=False`` disables the popular-element heuristic that keeps difflib
    usable on repetitive text — exactly what a system-prompt style instruction
    (many near-identical rule lines) is. Measured on a customer workspace: one
    call on a 20k-character instruction took 66.7s and was 100% of
    ``GET /api/instructions/{id}`` (SQL: 0.025s). Because this runs on the event
    loop, it also starved every other request on the worker — an unrelated
    instruction-list query went 0.076s idle -> 55.7s alongside it.

    The scan answers the docstring's question exactly; difflib only approximated
    it. Its greedy longest-match alignment can emit a ``delete`` even when a
    subsequence embedding exists, so it could answer False where the contract
    says True — never the reverse, since an alignment with no delete/replace
    leaves every character of ``small`` in an ``equal`` block, which *is* a
    subsequence. So this can only ever return True where difflib returned False
    (a suggestion main already contains being correctly recognised as covered),
    and 1,000 randomised edit pairs (insert / append / delete / replace /
    line-reorder / identical) produced no disagreement at all. Cost on the
    pathological pair above: 66.7s -> 0.7ms.

    See ``docs/feedback-loops/agents-pending-reconciliation-perf.md`` for the
    sibling diff in ``text_hunks`` — capped at ``MAX_DIFF_TOKENS`` and run off
    the event loop. Neither guard reached here: this one compares raw characters
    rather than word tokens, and no caller offloads it."""
    small = small or ""
    big = big or ""
    if small == big:
        return False
    if not small:
        return True  # the empty proposal is contained in anything non-empty
    if len(small) > len(big):
        return False  # a longer string cannot be a subsequence of a shorter one
    # `ch in it` consumes the iterator up to the match, so each character of
    # `big` is visited at most once across the whole comprehension.
    it = iter(big)
    return all(ch in it for ch in small)


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
    additive sibling's text happens to contain its (shorter) text.

    This is O(candidates^2) in ``covers`` calls, which was ruinous while each one
    was a quadratic character diff. Each is now a linear scan, and the length
    test below prunes the pairs that cannot match to O(1): ``b`` must be strictly
    longer than ``a`` to add anything, since a subsequence of equal length is the
    string itself (which ``covers`` reports as False)."""
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
            if len(b_text or "") <= len(a_text or ""):
                continue  # cannot be a strict superset — skip before scanning
            if covers(a_text or "", b_text or ""):   # b ⊋ a → a is intermediate
                superseded.add(a)
                break
    return superseded
