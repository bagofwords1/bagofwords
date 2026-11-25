"""The query spec both SharePoint lists connectors accept.

`sharepoint_lists` (Microsoft Graph, SharePoint Online) and
`sharepoint_onprem_lists` (SharePoint Server REST) take the same JSON spec, so a
model that learned one can use the other:

    {"list": "Expenses", "filter": "Category eq 'Travel'",
     "select": ["Title", "Amount"], "orderby": "Modified desc", "limit": 1000}

Columns are named as the catalog shows them (display names). Each column is a
dict with at least ``internal`` and ``display``; the connectors add their own
keys.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional


def parse_list_spec(query) -> dict:
    """A spec dict from a JSON string, a dict, or a bare list name."""
    if isinstance(query, dict):
        return dict(query)
    text = str(query or "").strip()
    if text.startswith("{"):
        try:
            spec = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON query spec: {e}")
        if not isinstance(spec, dict):
            raise ValueError("Query spec must be a JSON object.")
        return spec
    # A bare string is treated as a list name — fetch everything.
    return {"list": text}


def column_lookup(cols: List[dict]):
    """A resolver: display or internal name (case-insensitive) → column dict."""
    by_display: Dict[str, dict] = {}
    by_internal: Dict[str, dict] = {}
    for c in cols:
        by_display.setdefault(c["display"].casefold(), c)
        by_internal.setdefault(c["internal"].casefold(), c)
        if c.get("entity"):
            by_internal.setdefault(str(c["entity"]).casefold(), c)

    def find(ref: str) -> Optional[dict]:
        r = (ref or "").strip().casefold()
        return by_display.get(r) or by_internal.get(r)

    return find


def select_columns(raw_select, cols: List[dict], list_label: str) -> List[dict]:
    """The spec's `select` resolved to columns, in the order given; all when absent."""
    if not raw_select:
        return list(cols)
    if isinstance(raw_select, str):
        raw_select = [s.strip() for s in raw_select.split(",") if s.strip()]
    find = column_lookup(cols)
    selected, unknown = [], []
    for ref in raw_select:
        c = find(str(ref))
        if c is None:
            unknown.append(ref)
        elif c not in selected:
            selected.append(c)
    if unknown:
        known = ", ".join(c["display"] for c in cols)
        raise ValueError(
            f"Unknown column(s) {unknown} on list '{list_label}'. "
            f"Available columns: {known}"
        )
    return selected


def spec_limit(spec: dict, default: int, ceiling: int) -> int:
    """The spec's row limit, clamped to [1, ceiling]."""
    try:
        limit = int(spec.get("limit") or default)
    except (TypeError, ValueError):
        raise ValueError("'limit' must be a whole number.")
    return max(1, min(limit, ceiling))
