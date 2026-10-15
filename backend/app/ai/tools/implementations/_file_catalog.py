"""Catalog-backed file listing: filter, sort and page stored catalog rows.

Pure functions over the per-connection catalog (ConnectionTable rows whose
metadata_json carries a file-connector block). No I/O here so the paging
contract can be unit-tested without a database.
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.data_sources.clients._sharepoint_listing import matches_time_bounds, time_bounds

# Connector-specific metadata keys on a catalog row's metadata_json.
FILE_METADATA_KEYS = ("graph", "sharepoint_onprem", "documentum", "google_drive", "network_dir", "s3")

SORT_FIELDS = ("name", "modified_at", "created_at", "size")


class CursorError(ValueError):
    """The continuation token does not belong to this listing."""


def entries_from_tables(tables: Iterable[Any]) -> List[Dict[str, Any]]:
    """Catalog rows → listing entries. Rows without a file block are skipped."""
    out = []
    for t in tables or []:
        meta_json = getattr(t, "metadata_json", None) or {}
        if not isinstance(meta_json, dict):
            continue
        sub = next((meta_json.get(k) for k in FILE_METADATA_KEYS if isinstance(meta_json.get(k), dict)), None)
        if sub is None:
            continue
        name = str(getattr(t, "name", "") or "")
        out.append({
            "id": sub.get("file_id") or name,
            "name": name.rsplit("/", 1)[-1],
            "path": name,
            "mime_type": sub.get("mime_type"),
            "size": sub.get("size"),
            "modified_at": sub.get("modified_at"),
            "created_at": sub.get("created_at"),
            "web_url": sub.get("web_url"),
        })
    return out


def _in_folder(entry: Dict[str, Any], folder: str, recursive: bool) -> bool:
    """`folder` is either a catalog-relative path ("Finance/2026") or the
    connector's own folder id (a server-relative URL for SharePoint, matched
    against the file id). Empty folder = the whole scope."""
    if not folder:
        return True
    folder = folder.rstrip("/")
    for value in (entry.get("path") or "", str(entry.get("id") or "")):
        if value.lower().startswith(folder.lower() + "/"):
            rest = value[len(folder) + 1:]
            return recursive or "/" not in rest
    return False


def _fingerprint(**scope) -> str:
    return hashlib.sha1(json.dumps(scope, sort_keys=True, default=str).encode()).hexdigest()[:16]


def encode_cursor(offset: int, fingerprint: str) -> str:
    raw = json.dumps({"o": offset, "k": fingerprint}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, fingerprint: str) -> int:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        offset, key = int(data["o"]), str(data["k"])
    except Exception as exc:
        raise CursorError("cursor is not a continuation token from a previous list_files call.") from exc
    if key != fingerprint or offset < 0:
        raise CursorError("cursor belongs to a different listing (connection, folder, filters, sort order "
                          "or catalog version changed). Start again without a cursor.")
    return offset


def page_catalog(
    entries: List[Dict[str, Any]], *, scope_key: str, folder_id: Optional[str] = None,
    recursive: bool = False, name_pattern: Optional[str] = None,
    created_after=None, created_before=None, modified_after=None, modified_before=None,
    sort_by: str = "name", sort_order: str = "asc", limit: int = 50, cursor: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], int, Optional[str]]:
    """Filter, sort and page catalog entries.

    Returns (page, matched_total, next_cursor). The cursor is bound to the
    connection/catalog version (`scope_key`) plus every filter and the sort
    order, so a continuation under different inputs is rejected instead of
    silently returning a page of a different listing.
    """
    if sort_by not in SORT_FIELDS:
        raise ValueError(f"sort_by must be one of {', '.join(SORT_FIELDS)}.")
    if sort_order not in ("asc", "desc"):
        raise ValueError("sort_order must be asc or desc.")
    bounds = time_bounds(created_after=created_after, created_before=created_before,
                         modified_after=modified_after, modified_before=modified_before)
    folder = (folder_id or "").strip()
    pattern = (name_pattern or "").lower()
    matched = []
    for e in entries:
        if not _in_folder(e, folder, recursive):
            continue
        if pattern and not (fnmatch.fnmatch(str(e.get("name") or "").lower(), pattern)
                            or fnmatch.fnmatch(str(e.get("path") or "").lower(), pattern)):
            continue
        if not matches_time_bounds(e, bounds):
            continue
        matched.append(e)

    def key(e):
        value = e.get(sort_by)
        # Files without the sort field sort last in either direction; path breaks ties.
        missing = value is None or value == ""
        if sort_by == "name":
            value = str(e.get("path") or "").lower()
        elif sort_by == "size":
            value = int(value or 0)
        else:
            value = str(value or "")
        return (missing, value, str(e.get("path") or "").lower())

    matched.sort(key=key, reverse=(sort_order == "desc"))
    if sort_order == "desc":
        # Keep "missing last" regardless of direction.
        present = [e for e in matched if not key(e)[0]]
        matched = present + [e for e in matched if key(e)[0]]

    fingerprint = _fingerprint(scope=scope_key, folder=folder.lower(), recursive=bool(recursive),
                               pattern=pattern, bounds={f"{k[0]}:{k[1]}": v for k, v in bounds.items()},
                               sort_by=sort_by, sort_order=sort_order)
    offset = decode_cursor(cursor, fingerprint) if cursor else 0
    page = matched[offset:offset + limit]
    next_cursor = encode_cursor(offset + limit, fingerprint) if offset + limit < len(matched) else None
    return page, len(matched), next_cursor
