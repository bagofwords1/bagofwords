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


def _validate_sort(sort_by: str, sort_order: str) -> None:
    if sort_by not in SORT_FIELDS:
        raise ValueError(f"sort_by must be one of {', '.join(SORT_FIELDS)}.")
    if sort_order not in ("asc", "desc"):
        raise ValueError("sort_order must be asc or desc.")


def _filter_entries(entries, *, folder: str, recursive: bool, pattern: str, bounds) -> List[Dict[str, Any]]:
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
    return matched


def _sort_rows(rows: List[Dict[str, Any]], sort_field: str, sort_order: str, *, numeric: bool,
               tiebreak: str = "path", name_sort: bool = False) -> List[Dict[str, Any]]:
    """Sort rows by `sort_field`; rows lacking it sort last in either direction."""
    def key(e):
        value = e.get(sort_field)
        missing = value is None or value == ""
        if name_sort:
            value = str(e.get(tiebreak) or "").lower()
        elif numeric:
            value = int(value or 0)
        else:
            value = str(value or "")
        return (missing, value, str(e.get(tiebreak) or "").lower())

    rows = sorted(rows, key=key, reverse=(sort_order == "desc"))
    if sort_order == "desc":
        # Keep "missing last" regardless of direction.
        rows = [e for e in rows if not key(e)[0]] + [e for e in rows if key(e)[0]]
    return rows


def _page(rows: List[Dict[str, Any]], fingerprint: str, limit: int, cursor: Optional[str]):
    offset = decode_cursor(cursor, fingerprint) if cursor else 0
    page = rows[offset:offset + limit]
    next_cursor = encode_cursor(offset + limit, fingerprint) if offset + limit < len(rows) else None
    return page, next_cursor


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
    _validate_sort(sort_by, sort_order)
    bounds = time_bounds(created_after=created_after, created_before=created_before,
                         modified_after=modified_after, modified_before=modified_before)
    folder = (folder_id or "").strip()
    pattern = (name_pattern or "").lower()
    matched = _filter_entries(entries, folder=folder, recursive=recursive, pattern=pattern, bounds=bounds)
    matched = _sort_rows(matched, sort_by, sort_order, numeric=(sort_by == "size"), name_sort=(sort_by == "name"))
    fingerprint = _fingerprint(scope=scope_key, folder=folder.lower(), recursive=bool(recursive),
                               pattern=pattern, bounds={f"{k[0]}:{k[1]}": v for k, v in bounds.items()},
                               sort_by=sort_by, sort_order=sort_order)
    page, next_cursor = _page(matched, fingerprint, limit, cursor)
    return page, len(matched), next_cursor


#: Sample file names carried on each folder row.
FOLDER_SAMPLE_FILES = 3

# Folder rows sort by the aggregate that matches the file sort key.
_FOLDER_SORT_FIELDS = {"name": "path", "modified_at": "latest_modified_at",
                       "created_at": "earliest_created_at", "size": "file_count"}


def _relative_parts(entry: Dict[str, Any], folder: str) -> List[str]:
    """Path segments of `entry` below `folder` (the whole path when no folder)."""
    path = str(entry.get("path") or "")
    if not folder:
        return [p for p in path.split("/") if p]
    folder = folder.rstrip("/")
    for value in (path, str(entry.get("id") or "")):
        if value.lower().startswith(folder.lower() + "/"):
            return [p for p in value[len(folder) + 1:].split("/") if p]
    return []


def group_catalog_folders(
    entries: List[Dict[str, Any]], *, scope_key: str, folder_id: Optional[str] = None, depth: int = 1,
    name_pattern: Optional[str] = None,
    created_after=None, created_before=None, modified_after=None, modified_before=None,
    sort_by: str = "name", sort_order: str = "asc", limit: int = 300, cursor: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, int], Optional[str]]:
    """Group the filtered catalog into folders `depth` levels below the scope.

    Filters (folder, name_pattern, dates) apply to files first, always
    recursively; each matched file then counts toward its ancestor folder at
    `depth`. Files sitting above that depth belong to no folder and are only
    counted. The catalog holds files only, so a folder with no matching file
    does not appear.

    Returns (page, stats, next_cursor) where stats has `folders` (total
    folders matched), `files` (files matched) and `files_outside_folders`.
    sort_by maps onto folder aggregates: name → path, modified_at → latest
    modified, created_at → earliest created, size → file count.
    """
    _validate_sort(sort_by, sort_order)
    if int(depth) < 1:
        raise ValueError("depth must be 1 or more.")
    depth = int(depth)
    bounds = time_bounds(created_after=created_after, created_before=created_before,
                         modified_after=modified_after, modified_before=modified_before)
    folder = (folder_id or "").strip()
    pattern = (name_pattern or "").lower()
    matched = _filter_entries(entries, folder=folder, recursive=True, pattern=pattern, bounds=bounds)

    groups: Dict[str, Dict[str, Any]] = {}
    outside = 0
    for e in matched:
        rel = _relative_parts(e, folder)
        if len(rel) <= depth:  # the file itself sits above the requested folder level
            outside += 1
            continue
        parts = [p for p in str(e.get("path") or "").split("/") if p]
        # Catalog-relative folder path: the entry path minus everything below depth.
        cut = len(parts) - (len(rel) - depth)
        path = "/".join(parts[:cut]) if cut > 0 else "/".join(rel[:depth])
        g = groups.get(path.lower())
        if g is None:
            g = groups[path.lower()] = {"path": path, "name": path.rsplit("/", 1)[-1], "file_count": 0,
                                        "latest_modified_at": None, "earliest_created_at": None,
                                        "sample_files": []}
        g["file_count"] += 1
        mod, created = e.get("modified_at"), e.get("created_at")
        if mod and (g["latest_modified_at"] is None or str(mod) > str(g["latest_modified_at"])):
            g["latest_modified_at"] = mod
        if created and (g["earliest_created_at"] is None or str(created) < str(g["earliest_created_at"])):
            g["earliest_created_at"] = created
        if len(g["sample_files"]) < FOLDER_SAMPLE_FILES:
            g["sample_files"].append(str(e.get("name") or ""))

    field = _FOLDER_SORT_FIELDS[sort_by]
    rows = _sort_rows(list(groups.values()), field, sort_order, numeric=(sort_by == "size"),
                      name_sort=(sort_by == "name"))
    fingerprint = _fingerprint(scope=scope_key, group_by="folder", depth=depth, folder=folder.lower(),
                               pattern=pattern, bounds={f"{k[0]}:{k[1]}": v for k, v in bounds.items()},
                               sort_by=sort_by, sort_order=sort_order)
    page, next_cursor = _page(rows, fingerprint, limit, cursor)
    stats = {"folders": len(rows), "files": len(matched), "files_outside_folders": outside}
    return page, stats, next_cursor
