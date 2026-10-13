"""Field-level (non-text) changes a suggestion proposes to an instruction.

The per-hunk review (``InstructionService.review_hunks``) diffs a suggestion's
TEXT against main. A suggestion can also change a field that has no text at
all — most importantly ``status`` (Active ↔ Inactive), which is what takes an
instruction in or out of every context loader. Those changes used to be
invisible to review: a status-only suggestion produced zero hunks, so it showed
no pending badge, the chat card read it as "accepted", and accepting text hunks
rebuilt the version from MAIN's metadata, silently dropping it.

This module is the pure half of the fix: given the base, proposed and main
version metadata, which field changes are still live to review. It follows the
same 3-way rule as text hunks — a change is the suggestion's INTENT
(base → proposed), and it is live only while main does not already carry it and
no reviewer has resolved it. Keys share the build's ``rejected_hunks`` ledger
with text hunks; the ``field:`` prefix keeps them disjoint from the 16-char hex
text-hunk keys and the ``__settled__``/``__voided__`` markers.
"""
from typing import Dict, Iterable, List, Optional

FIELD_KEY_PREFIX = "field:"

# Reviewable fields and the values a suggestion may move them to. ``archived``
# is deliberately absent: it is a delete-like lifecycle state owned by the
# explicit archive flow, not something a suggestion can propose.
REVIEWED_FIELDS: Dict[str, tuple] = {
    "status": ("published", "draft"),
    "load_mode": ("always", "intelligent", "disabled"),
}

# What a NULL column means on an instruction version (see InstructionVersion).
_DEFAULTS = {"status": "published", "load_mode": "always"}


def field_meta(status: Optional[str], load_mode: Optional[str]) -> Dict[str, str]:
    """Normalized reviewable metadata of one version."""
    return {
        "status": status or _DEFAULTS["status"],
        "load_mode": load_mode or _DEFAULTS["load_mode"],
    }


def field_change_key(field: str, to: str) -> str:
    """Stable review key: binds the field AND the proposed value, so a later
    proposal of a different value is a new decision, not a resolved one."""
    return f"{FIELD_KEY_PREFIX}{field}:{to}"


def is_field_key(key: str) -> bool:
    return isinstance(key, str) and key.startswith(FIELD_KEY_PREFIX)


def field_change_summary(change: dict) -> str:
    """One-line description for verdict events and agent-facing summaries."""
    return f"{change['field']}: {change['from']} -> {change['to']}"


def has_field_intent(base: Optional[dict], proposed: Optional[dict]) -> bool:
    """Whether the suggestion changes any reviewed field relative to its base,
    regardless of main or recorded resolutions."""
    if proposed is None or base is None:
        return False
    return any(base.get(f) != proposed.get(f) for f in REVIEWED_FIELDS)


def field_changes_against_main(
    base: Optional[dict],
    proposed: Optional[dict],
    main: Optional[dict],
    resolved_keys: Iterable[str] = (),
) -> List[dict]:
    """Field changes this suggestion still proposes against current main.

    ``main`` is None when the instruction is not live yet (a create
    suggestion): there is no field to change, the whole instruction is the
    proposal and is reviewed as text. ``base`` may be None when the
    suggestion's baseline is unknown; then every difference from main counts.
    """
    if proposed is None or main is None:
        return []
    resolved = set(resolved_keys or ())
    out: List[dict] = []
    for field, allowed in REVIEWED_FIELDS.items():
        to = proposed.get(field)
        if to not in allowed:
            continue
        frm = main.get(field)
        if to == frm:
            continue  # main already carries it
        if base is not None and base.get(field) == to:
            continue  # carried over from the base, not this suggestion's intent
        key = field_change_key(field, to)
        if key in resolved:
            continue
        out.append({"key": key, "field": field, "from": frm, "to": to})
    return out
