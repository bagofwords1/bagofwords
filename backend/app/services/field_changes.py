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

``scope`` — sharing the instruction with more agents — is reviewed here too,
but it is never inferred: a version's ``data_source_ids`` is a snapshot of the
live attachment at write time, and comparing two snapshots reads the leftovers
of older publishes as proposals. Only a BUILD ROW that carries an explicit
``scope_change`` (BuildContent.scope_change, written by edit_instruction's
add_agents) proposes one, and what it proposes is the agents it names that
main is still missing. It sits on the build row, not the version, so that it
lives and dies with the suggestion that made it (see BuildContent).
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


def scope_add_ids(scope_change: Optional[dict]) -> tuple:
    """The agent ids a version's ``scope_change`` asks to add, normalized."""
    if not isinstance(scope_change, dict):
        return ()
    add = scope_change.get("add") or []
    return tuple(sorted({str(d) for d in add if d}))


def scope_refs_for(scope_change: Optional[dict], agent_ids: Iterable[str]) -> List[dict]:
    """The table references a ``scope_change`` carries for ``agent_ids``.

    Refs are stored on the change itself ({"add": [...], "refs": [{...,
    "data_source_id": ...}]}), on the build row, never in a version's
    snapshot: a snapshot travels into main and into other builds, and the
    tables of a share — accepted or rejected — must not."""
    if not isinstance(scope_change, dict):
        return []
    wanted = {str(d) for d in agent_ids}
    return [r for r in (scope_change.get("refs") or [])
            if isinstance(r, dict) and str(r.get("data_source_id")) in wanted]


def field_meta(status: Optional[str], load_mode: Optional[str]) -> Dict[str, object]:
    """Normalized reviewable metadata of one version."""
    return {
        "status": status or _DEFAULTS["status"],
        "load_mode": load_mode or _DEFAULTS["load_mode"],
        "scope_add": (),
    }


def with_scope(meta: Optional[dict], scope_change: Optional[dict]) -> Optional[dict]:
    """``meta`` of a proposed version plus the share its BUILD ROW proposes
    (BuildContent.scope_change). The share is a property of the suggestion,
    not of the version, so it is overlaid here rather than read from one."""
    if meta is None:
        return None
    return {**meta, "scope_add": scope_add_ids(scope_change)}


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


def has_field_intent(base: Optional[dict], proposed: Optional[dict]) -> bool:
    """Whether the suggestion changes any reviewed field relative to its base,
    regardless of main or recorded resolutions."""
    if proposed is None or base is None:
        return False
    if any(base.get(f) != proposed.get(f) for f in REVIEWED_FIELDS):
        return True
    return bool(proposed.get("scope_add"))


def scope_change_key(agent_id: str) -> str:
    """Review key of sharing with ONE agent. Each agent is its own decision:
    rejecting B stays rejected when the session later adds D, and accepting
    D never drags B along."""
    return field_change_key(SCOPE_FIELD, hashlib.sha1(str(agent_id).encode()).hexdigest()[:16])


def unresolved_scope_adds(add: Iterable[str], live: Iterable[str],
                          resolved_keys: Iterable[str] = ()) -> List[str]:
    """The agents of a share still waiting on a reviewer: proposed, not yet
    attached, and not rejected. The ONE rule behind the review row, the
    pending badge and every publish guard, so they can never disagree.

    An empty ``live`` is a global instruction — it already reaches every
    agent, so nothing is left to share and nothing is pending. (Attaching an
    agent to a global instruction would NARROW it to that agent alone.)"""
    live = {str(d) for d in live}
    if not live:
        return []
    resolved = set(resolved_keys or ())
    return sorted(
        str(a) for a in add
        if str(a) not in live and scope_change_key(a) not in resolved
    )


def _scope_changes(proposed: dict, main: dict, resolved: set) -> List[dict]:
    add = proposed.get("scope_add") or ()
    if not add:
        return []
    live = tuple(main.get("scope_live") or ())
    return [
        {"key": scope_change_key(agent_id), "field": SCOPE_FIELD, "from": list(live),
         "to": sorted(set(live) | {agent_id}), "added": [agent_id]}
        for agent_id in unresolved_scope_adds(add, live, resolved)
    ]


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
    ``main["scope_live"]`` is the live attachment (the agents that load the
    instruction now), which a scope change is measured against.
    """
    if proposed is None or main is None:
        return []
    resolved = set(resolved_keys or ())
    out: List[dict] = list(_scope_changes(proposed, main, resolved))
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
