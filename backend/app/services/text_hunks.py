"""Word-level text hunks — the shared, server-authoritative diff + 3-way apply
used by the per-hunk tracked-changes review (immutable cherry-pick model).

A *hunk* is the unit of accept/reject. Hunks are computed as a suggestion's
**intent**: ``diff(base_text, proposed_text)`` — the changes the suggestion
actually makes, independent of how `main` has moved since. To accept a hunk it
is **applied onto current main** by a token-level 3-way merge (the hunk's base
token range is located in main via the longest-matching alignment; if main has
changed that region it's a conflict). This avoids surfacing a frozen snapshot's
stale values as spurious changes.

Granularity is word-level (words / whitespace / single symbols), matching the
frontend renderer.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

import difflib

_TOKEN_RE = re.compile(r"\w+|\s+|[^\w\s]")

EQUAL, DELETE, INSERT = 0, -1, 1


def _tokenize(s: str) -> List[str]:
    return _TOKEN_RE.findall(s or "")


def diff_word_ops(a: str, b: str) -> List[Tuple[int, str]]:
    """Word-level diff of a->b as (op_type, text). Used to render a hunk against
    current main (EQUAL/DELETE reconstruct `a`; EQUAL/INSERT reconstruct `b`)."""
    if a == b:
        return [(EQUAL, a)] if a else []
    ta, tb = _tokenize(a), _tokenize(b)
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    ops: List[Tuple[int, str]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            ops.append((EQUAL, "".join(ta[i1:i2])))
        elif tag == "delete":
            ops.append((DELETE, "".join(ta[i1:i2])))
        elif tag == "insert":
            ops.append((INSERT, "".join(tb[j1:j2])))
        else:
            ops.append((DELETE, "".join(ta[i1:i2])))
            ops.append((INSERT, "".join(tb[j1:j2])))
    return ops


@dataclass
class Hunk:
    index: int
    ops: List[Tuple[int, str]] = field(default_factory=list)  # (type, text) for rendering
    before: str = ""           # text removed (base side)
    after: str = ""            # text added (proposed side)
    left_context: str = ""     # equal text immediately before
    base_lo: int = 0           # base token index range [lo, hi) this hunk replaces
    base_hi: int = 0
    after_tokens: List[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        raw = f"{self.before}\x00{self.after}\x00{self.left_context[-24:]}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "key": self.key,
            "before": self.before,
            "after": self.after,
            "ops": [{"type": t, "text": txt} for t, txt in self.ops],
        }


@dataclass
class RebasedHunkCache:
    """Request-local memoization for a batch of tracked-change comparisons."""

    intents: dict[tuple[str, str], List[Hunk]] = field(default_factory=dict)
    alignments: dict[
        tuple[str, str],
        tuple[List[str], List[str], List[Optional[int]], List[int]],
    ] = field(default_factory=dict)


def compute_hunks(base: str, proposed: str) -> List[Hunk]:
    """The suggestion's intent: hunks transforming base -> proposed (word-level).
    Each hunk records the base token range it replaces so it can be applied onto
    a possibly-moved `main`."""
    if (base or "") == (proposed or ""):
        return []
    ta, tb = _tokenize(base), _tokenize(proposed)
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    hunks: List[Hunk] = []
    cur: Optional[Hunk] = None
    last_equal = ""
    idx = -1
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            cur = None
            last_equal = "".join(ta[i1:i2])
            continue
        if cur is None:
            idx += 1
            cur = Hunk(index=idx, left_context=last_equal, base_lo=i1, base_hi=i1)
            hunks.append(cur)
        if tag in ("delete", "replace"):
            cur.before += "".join(ta[i1:i2])
            cur.ops.append((DELETE, "".join(ta[i1:i2])))
            cur.base_hi = i2
        if tag in ("insert", "replace"):
            ins = "".join(tb[j1:j2])
            cur.after += ins
            cur.ops.append((INSERT, ins))
            cur.after_tokens.extend(tb[j1:j2])
    return hunks


def _base_to_main_positions(ta: List[str], tm: List[str]) -> List[Optional[int]]:
    """For each base token boundary 0..len(ta), the corresponding main boundary,
    or None where the alignment breaks (main changed around it)."""
    pos: List[Optional[int]] = [None] * (len(ta) + 1)
    for a, b, size in difflib.SequenceMatcher(None, ta, tm, autojunk=False).get_matching_blocks():
        for k in range(size + 1):
            if a + k <= len(ta):
                pos[a + k] = b + k
    return pos


def apply_hunk_onto(base: str, main: str, hunk: Hunk) -> Tuple[str, bool]:
    """Apply one intent hunk (base->proposed) onto `main` via a localized 3-way
    merge. Returns (new_main, ok):
      - ok=True, new_main==main  → the change is already present in main (no-op);
      - ok=True, new_main!=main  → applied cleanly;
      - ok=False                 → main changed the hunk's region (conflict).
    """
    ta, tm = _tokenize(base), _tokenize(main)
    pos = _base_to_main_positions(ta, tm)
    lo, hi = hunk.base_lo, hunk.base_hi
    mi1, mi2 = pos[lo], pos[hi]
    if mi1 is None or mi2 is None or mi2 < mi1:
        return main, False
    after = list(hunk.after_tokens)
    region = tm[mi1:mi2]
    base_region = ta[lo:hi]
    # Already applied: main's region is exactly the proposed text.
    if region == after:
        return main, True
    # Pure insertion already present immediately before/after the anchor (the
    # alignment can land the insert point on either side of main's copy).
    if lo == hi and after:
        n = len(after)
        if tm[max(0, mi1 - n):mi1] == after or tm[mi1:mi1 + n] == after:
            return main, True
    # Clean apply: main's region is unchanged from base → splice in the proposal.
    if region == base_region:
        return "".join(tm[:mi1] + after + tm[mi2:]), True
    # main changed this region differently → conflict.
    return main, False


def live_hunks_against_main(base: str, proposed: str, main: str) -> List[dict]:
    """The suggestion's hunks that are LIVE against current main — i.e. apply
    cleanly and actually change main (not already-applied, not conflicting) —
    each as {key, start, end, before, after} positioned in main.

    Computes the base->main token alignment ONCE for the whole suggestion and
    derives each hunk's position directly from it, so the cost is
    O(tokens^2 + hunks) instead of O(hunks * tokens^2). No second diff per hunk."""
    intent = compute_hunks(base, proposed)
    if not intent:
        return []
    ta, tm = _tokenize(base), _tokenize(main)
    pos = _base_to_main_positions(ta, tm)
    # char offset of each main token boundary
    offs = [0]
    for t in tm:
        offs.append(offs[-1] + len(t))
    out: List[dict] = []
    for h in intent:
        mi1, mi2 = pos[h.base_lo], pos[h.base_hi]
        if mi1 is None or mi2 is None or mi2 < mi1:
            continue  # conflict (alignment broke around the hunk)
        region = tm[mi1:mi2]
        after = list(h.after_tokens)
        base_region = ta[h.base_lo:h.base_hi]
        if region == after:
            continue  # already applied
        if h.base_lo == h.base_hi and after:
            n = len(after)
            if tm[max(0, mi1 - n):mi1] == after or tm[mi1:mi1 + n] == after:
                continue  # insertion already present at the anchor
        if region != base_region:
            continue  # main changed this region differently → conflict
        before = "".join(region)
        out.append({
            "key": h.key, "start": offs[mi1], "end": offs[mi1] + len(before),
            "before": before, "after": "".join(after),
        })
    return out


def rebased_hunks_against_main(
    base: str,
    proposed: str,
    main: str,
    *,
    cache: Optional[RebasedHunkCache] = None,
) -> List[dict]:
    """Lenient variant of `live_hunks_against_main`. Same intent hunks, positioned
    against current main, but instead of DROPPING a hunk whose base region drifted
    in main (the strict conflict rule), it surfaces it anyway with `before` taken
    from main's current region (proposed-wins on accept) — so a *stale* suggestion
    stays reviewable instead of collapsing to nothing.

    Crucially the hunk `key` is the intent hunk's key (derived from the immutable
    base side), so it is STABLE as main changes — a resolved hunk's key keeps
    matching `rejected_hunks` and won't re-surface. The already-applied / no-op
    skips are preserved, so when main hasn't drifted this returns exactly what
    `live_hunks_against_main` does (healthy suggestions unaffected)."""
    base = base or ""
    proposed = proposed or ""
    main = main or ""

    intent_key = (base, proposed)
    if cache is not None and intent_key in cache.intents:
        intent = cache.intents[intent_key]
    else:
        intent = compute_hunks(base, proposed)
        if cache is not None:
            cache.intents[intent_key] = intent
    if not intent:
        return []

    alignment_key = (base, main)
    alignment = cache.alignments.get(alignment_key) if cache is not None else None
    if alignment is None:
        ta = _tokenize(base)
        if base == main:
            tm = ta
            pos = list(range(len(ta) + 1))
        else:
            tm = _tokenize(main)
            pos = _base_to_main_positions(ta, tm)
        offs = [0]
        for t in tm:
            offs.append(offs[-1] + len(t))
        alignment = (ta, tm, pos, offs)
        if cache is not None:
            cache.alignments[alignment_key] = alignment
    ta, tm, pos, offs = alignment
    out: List[dict] = []
    for h in intent:
        mi1, mi2 = pos[h.base_lo], pos[h.base_hi]
        if mi1 is None or mi2 is None or mi2 < mi1:
            continue  # anchor gone from main entirely → can't place
        region = tm[mi1:mi2]
        after = list(h.after_tokens)
        if region == after:
            continue  # already applied
        if h.base_lo == h.base_hi and after:
            n = len(after)
            if tm[max(0, mi1 - n):mi1] == after or tm[mi1:mi1 + n] == after:
                continue  # insertion already present at the anchor
        before = "".join(region)
        after_str = "".join(after)
        if before == after_str:
            continue  # no net change against main
        out.append({"key": h.key, "start": offs[mi1], "end": offs[mi1] + len(before),
                    "before": before, "after": after_str})
    return out


def has_live_hunk_against_main(
    base: str,
    proposed: str,
    main: str,
    rejected_keys: Optional[Set[str]] = None,
    *,
    cache: Optional[RebasedHunkCache] = None,
) -> bool:
    """Whether a suggestion has an unrejected hunk that still changes main."""

    base = base or ""
    proposed = proposed or ""
    main = main or ""
    rejected = rejected_keys or set()

    if proposed == base or proposed == main:
        return False
    # NB: no `base == main -> return True` shortcut. Even when the suggestion
    # forked from current main, its change can still apply as a NO-OP against it
    # (an idempotent insertion of a token already sitting at the anchor, or an
    # already-present phrase). `rebased_hunks_against_main` performs exactly those
    # skips, so an eager True here would flag the instruction "Pending review"
    # while /review-hunks renders zero hunks — pending with nothing to review.
    # Fall through to the authoritative hunk computation instead (identity
    # alignment when base == main, so this stays cheap).
    return any(
        hunk["key"] not in rejected
        for hunk in rebased_hunks_against_main(base, proposed, main, cache=cache)
    )


def applied_text_for(base: str, proposed: str, hunk_index: int, onto_main: str) -> Tuple[Optional[str], bool]:
    """Convenience: recompute intent hunks of (base, proposed) and apply hunk
    `hunk_index` onto `onto_main`. Returns (new_main, ok); (None, False) if the
    index is out of range."""
    hunks = compute_hunks(base, proposed)
    if hunk_index < 0 or hunk_index >= len(hunks):
        return None, False
    new_main, ok = apply_hunk_onto(base, onto_main, hunks[hunk_index])
    return new_main, ok
