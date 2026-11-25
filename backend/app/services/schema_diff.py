"""Structured before/after diff of an agent's table catalog.

A schema-change notification used to carry only a list of up to eight changed
table names. This module computes what actually changed, so an admin can see
which tables were added or removed and, for a table that stayed, which columns
were added or removed and which changed type.

Pure functions only: the inputs are ``{table_name: columns}`` maps taken
before and after a sync. Lists are bounded so a large catalog can't bloat the
notification row; ``total`` keeps the real count and ``truncated`` says when a
list was cut.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional

MAX_TABLES = 50     # per list (added / removed / changed / new-inactive)
MAX_COLUMNS = 30    # per column list inside one changed table


def column_map(columns: Optional[Iterable[Any]]) -> Dict[str, str]:
    """``{column_name: type}`` from a catalog column list.

    Tolerates the shapes the catalog stores: dicts keyed ``dtype`` or ``type``,
    bare column names, and a missing list.
    """
    out: Dict[str, str] = {}
    for col in columns or []:
        if isinstance(col, dict):
            name = str(col.get("name", ""))
            out[name] = str(col.get("dtype", "") or col.get("type", "") or "")
        else:
            out[str(col)] = ""
    return out


def _table_change(table: str, before: Dict[str, str], after: Dict[str, str]) -> Optional[Dict[str, Any]]:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    retyped = sorted(c for c in set(before) & set(after) if before[c] != after[c])
    if not (added or removed or retyped):
        return None
    return {
        "table": table,
        "added_cols": [{"name": c, "type": after[c]} for c in added],
        "removed_cols": removed,
        "type_changes": [{"col": c, "from": before[c], "to": after[c]} for c in retyped],
    }


def compute_schema_diff(
    pre: Mapping[str, Any],
    post: Mapping[str, Any],
    *,
    new_inactive_tables: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Diff two ``{table_name: columns}`` snapshots of the tables an agent uses.

    ``new_inactive_tables`` are tables that appeared in the connection but were
    not activated for the agent. They are listed for context only and are not
    counted in ``total``: the agent can't query them, so nothing it answers
    has changed.
    """
    pre_cols = {t: column_map(c) for t, c in pre.items()}
    post_cols = {t: column_map(c) for t, c in post.items()}

    added_tables = sorted(set(post_cols) - set(pre_cols))
    removed_tables = sorted(set(pre_cols) - set(post_cols))
    changed: List[Dict[str, Any]] = []
    for t in sorted(set(pre_cols) & set(post_cols)):
        ch = _table_change(t, pre_cols[t], post_cols[t])
        if ch is not None:
            changed.append(ch)
    inactive = sorted(set(new_inactive_tables or []))

    total = len(added_tables) + len(removed_tables) + len(changed)
    truncated = False

    def cap(items: list, limit: int) -> list:
        nonlocal truncated
        if len(items) > limit:
            truncated = True
            return items[:limit]
        return items

    for ch in changed:
        ch["added_cols"] = cap(ch["added_cols"], MAX_COLUMNS)
        ch["removed_cols"] = cap(ch["removed_cols"], MAX_COLUMNS)
        ch["type_changes"] = cap(ch["type_changes"], MAX_COLUMNS)

    return {
        "added_tables": cap(added_tables, MAX_TABLES),
        "removed_tables": cap(removed_tables, MAX_TABLES),
        "changed": cap(changed, MAX_TABLES),
        "new_inactive_tables": cap(inactive, MAX_TABLES),
        "total": total,
        # Real counts, before truncation, for the summary line.
        "counts": {
            "added": len(added_tables),
            "removed": len(removed_tables),
            "modified": len(changed),
        },
        "truncated": truncated,
    }


def summary_line(diff: Mapping[str, Any]) -> str:
    """English fallback body for the notification (the inbox localizes it from
    ``subject.i18n``; this is what non-UI readers and old clients see)."""
    c = diff.get("counts") or {}
    parts = []
    if c.get("added"):
        parts.append(f"{c['added']} table(s) added")
    if c.get("removed"):
        parts.append(f"{c['removed']} removed")
    if c.get("modified"):
        parts.append(f"{c['modified']} changed")
    names = list(diff.get("added_tables") or []) + list(diff.get("removed_tables") or []) \
        + [ch["table"] for ch in diff.get("changed") or []]
    head = ", ".join(parts) or "Table/column structure changed"
    if names:
        shown = names[:8]
        more = int(diff.get("total") or len(names)) - len(shown)
        head += ": " + ", ".join(shown) + (f" (+{more} more)" if more > 0 else "")
    return head
