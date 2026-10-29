"""Classify a list schema edit as none / additive / breaking.

Breaking edits bump ``AgentList.version``; rows written under an older version
are then shown as stale. Fields are compared by their stable ``id`` so a rename
is not a removal.
"""
from typing import Any, Dict, List, Optional

_COSMETIC = ("name", "description", "unit", "method")


def classify_schema_change(
    old_fields: List[Dict[str, Any]],
    old_key: Optional[str],
    new_fields: List[Dict[str, Any]],
    new_key: Optional[str],
) -> str:
    old = {f["id"]: f for f in old_fields}
    new = {f["id"]: f for f in new_fields}
    breaking = False
    additive = False

    if (old_key or None) != (new_key or None):
        breaking = True

    for fid, of in old.items():
        nf = new.get(fid)
        if nf is None:
            breaking = True  # removed field
            continue
        if (of.get("type") or "string") != (nf.get("type") or "string"):
            breaking = True
        if not of.get("required") and nf.get("required"):
            breaking = True  # became required
        if of.get("required") and not nf.get("required"):
            additive = True
        old_enum = set(of.get("enum") or [])
        new_enum = set(nf.get("enum") or [])
        if old_enum - new_enum:
            breaking = True  # removed an allowed value
        elif new_enum - old_enum:
            additive = True
        if any((of.get(k) or None) != (nf.get(k) or None) for k in _COSMETIC):
            additive = True

    for fid, nf in new.items():
        if fid not in old:
            if nf.get("required"):
                breaking = True
            else:
                additive = True

    if [f["id"] for f in old_fields] != [f["id"] for f in new_fields]:
        additive = True  # reorder

    if breaking:
        return "breaking"
    return "additive" if additive else "none"
