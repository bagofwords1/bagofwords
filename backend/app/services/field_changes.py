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

``scope`` — the agents the instruction is attached to and the table references
it carries — is reviewed here too, as an ordinary version field. A version's
``data_source_ids`` and ``references_json`` ARE the proposed scope (the AI
``add_agents`` and ``table_names`` edits write them there, like every other
staged field), and they are diffed against the LIVE attachment
(``main['scope_live']`` / ``['scope_live_refs']`` — the association rows that
decide which agents load the instruction now). The 3-way rule keeps a stale
snapshot honest: a difference counts only when it is also a change from the
suggestion's own base, so a version that merely carries the live scope forward
proposes nothing. Accepting (or promoting the build) applies the base→proposed
delta to the live attachment; rejecting re-stages the base scope.
"""
import hashlib
from typing import Dict, Iterable, List, Optional

FIELD_KEY_PREFIX = "field:"
SCOPE_FIELD = "scope"

# Reviewable fields and the values a suggestion may move them to. ``archived``
# is deliberately absent: it is a delete-like lifecycle state owned by the
# explicit archive flow, not something a suggestion can propose.
REVIEWED_FIELDS: Dict[str, tuple] = {
    "status": ("published", "draft"),
    "load_mode": ("always", "intelligent", "disabled"),
}

# What a NULL column means on an instruction version (see InstructionVersion).
_DEFAULTS = {"status": "published", "load_mode": "always"}


def scope_ds(data_source_ids) -> tuple:
    """Normalized agent-id set of a version's (or the live row's) data sources."""
    return tuple(sorted({str(d) for d in (data_source_ids or []) if d}))


def _ref_ident(r: dict) -> tuple:
    return (r.get("object_type"), str(r.get("object_id")), r.get("column_name"))


def _clean_refs(references) -> List[dict]:
    """References normalized to the fields a snapshot carries, dropping blanks
    and the share-only ``data_source_id`` tag."""
    out = []
    for r in (references or []):
        if isinstance(r, dict) and r.get("object_id"):
            out.append({
                "object_type": r.get("object_type"),
                "object_id": str(r.get("object_id")),
                "column_name": r.get("column_name"),
                "relation_type": r.get("relation_type"),
                "display_text": r.get("display_text"),
            })
    return out


def scope_delta(from_ds, from_refs, to_ds, to_refs):
    """What changed from one scope to another:
    ``(added_ds, removed_ds, added_refs, removed_refs)``. Reference identity is
    (object_type, object_id, column_name)."""
    fds, tds = set(scope_ds(from_ds)), set(scope_ds(to_ds))
    added_ds = sorted(tds - fds)
    removed_ds = sorted(fds - tds)
    f_ident = {_ref_ident(r) for r in _clean_refs(from_refs)}
    t_ident = {_ref_ident(r) for r in _clean_refs(to_refs)}
    added_refs = [r for r in _clean_refs(to_refs) if _ref_ident(r) not in f_ident]
    removed_refs = [r for r in _clean_refs(from_refs) if _ref_ident(r) not in t_ident]
    return added_ds, removed_ds, added_refs, removed_refs


def field_meta(status: Optional[str], load_mode: Optional[str],
               data_source_ids=None, references=None) -> Dict[str, object]:
    """Normalized reviewable metadata of one version — the scalar fields plus
    the scope the version snapshots (its agents and table references)."""
    return {
        "status": status or _DEFAULTS["status"],
        "load_mode": load_mode or _DEFAULTS["load_mode"],
        "scope_ds": scope_ds(data_source_ids),
        "scope_refs": _clean_refs(references),
    }


def merge_refs(base: Optional[List[dict]], extra: Iterable[dict]) -> List[dict]:
    """``base`` plus every ref in ``extra`` not already there, identity being
    (object_type, object_id, column_name). Keeps ``base`` order; drops the
    share-only ``data_source_id`` tag from the appended refs."""
    out = list(base or [])
    have = {(r.get("object_type"), str(r.get("object_id")), r.get("column_name")) for r in out}
    for r in extra or []:
        ident = (r.get("object_type"), str(r.get("object_id")), r.get("column_name"))
        if not r.get("object_id") or ident in have:
            continue
        have.add(ident)
        out.append({"object_type": r.get("object_type"), "object_id": str(r.get("object_id")),
                    "column_name": r.get("column_name"), "display_text": r.get("display_text")})
    return out


def field_change_key(field: str, to: str) -> str:
    """Stable review key: binds the field AND the proposed value, so a later
    proposal of a different value is a new decision, not a resolved one."""
    return f"{FIELD_KEY_PREFIX}{field}:{to}"


def is_field_key(key: str) -> bool:
    return isinstance(key, str) and key.startswith(FIELD_KEY_PREFIX)


def field_change_summary(change: dict) -> str:
    """One-line description for verdict events and agent-facing summaries."""
    if change.get("field") == SCOPE_FIELD:
        def _side(names, ids):
            return ", ".join(names if names is not None else ids) or "all agents"
        return (f"agents: {_side(change.get('from_names'), change['from'])}"
                f" -> {_side(change.get('to_names'), change['to'])}")
    return f"{change['field']}: {change['from']} -> {change['to']}"


def scope_change_key(to_ds, to_refs=()) -> str:
    """Stable review key for a scope change, binding the proposed agents AND
    tables: a later proposal of a different scope is a new decision, and the
    same proposal keeps the same key across reloads."""
    parts = list(scope_ds(to_ds)) + sorted(
        str(r.get("object_id")) for r in (to_refs or [])
        if isinstance(r, dict) and r.get("object_id"))
    return field_change_key(SCOPE_FIELD, hashlib.sha1("|".join(parts).encode()).hexdigest()[:16])


def has_field_intent(base: Optional[dict], proposed: Optional[dict]) -> bool:
    """Whether the suggestion changes any reviewed field relative to its base,
    regardless of main or recorded resolutions."""
    if proposed is None or base is None:
        return False
    if any(base.get(f) != proposed.get(f) for f in REVIEWED_FIELDS):
        return True
    a, b, c, d = scope_delta(base.get("scope_ds"), base.get("scope_refs"),
                             proposed.get("scope_ds"), proposed.get("scope_refs"))
    return bool(a or b or c or d)


def _scope_change(base: Optional[dict], proposed: dict, main: dict, resolved: set) -> List[dict]:
    """The scope change this suggestion still proposes against the LIVE
    attachment, as one review row (agents + their table references), or [].

    A move FROM global (empty live attachment) is a real change too: narrowing
    a rule that reached every agent down to one is exactly the kind of scope
    change a reviewer must see and promotion will apply, so it is not
    short-circuited here — only a genuine no-op (proposed == base, or proposed
    already matches live) returns []."""
    live_ds = tuple(main.get("scope_live") or ())
    live_refs = main.get("scope_live_refs") or []
    p_ds = proposed.get("scope_ds") or ()
    p_refs = proposed.get("scope_refs") or []
    # Intent is base -> proposed: a version that merely carries the live scope
    # forward (same as its base) proposes nothing, even if main looks different.
    if base is not None:
        ba, br, bar, brr = scope_delta(base.get("scope_ds"), base.get("scope_refs"), p_ds, p_refs)
        if not (ba or br or bar or brr):
            return []
    added_ds, removed_ds, added_refs, removed_refs = scope_delta(live_ds, live_refs, p_ds, p_refs)
    if not (added_ds or removed_ds or added_refs or removed_refs):
        return []  # main already carries it
    key = scope_change_key(p_ds, p_refs)
    if key in resolved:
        return []
    to = sorted((set(live_ds) | set(added_ds)) - set(removed_ds))
    return [{
        "key": key, "field": SCOPE_FIELD,
        "from": list(live_ds), "to": to,
        "added": added_ds, "removed": removed_ds,
        "added_refs": added_refs, "removed_refs": removed_refs,
    }]


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
    ``main["scope_live"]`` / ``["scope_live_refs"]`` are the live attachment
    (the agents and tables that load the instruction now), which a scope change
    is measured against.
    """
    if proposed is None or main is None:
        return []
    resolved = set(resolved_keys or ())
    out: List[dict] = list(_scope_change(base, proposed, main, resolved))
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
