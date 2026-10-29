"""Catalog-backed list_files paging contract (pure, no DB).

Promises: folder scoping by relative path or connector id, recursive on/off,
glob on name or path, date bounds, deterministic sort with missing values
last, cursor continuation that yields every row exactly once, and rejection
of a cursor that belongs to a different listing.
"""
from types import SimpleNamespace

import pytest

from app.ai.tools.implementations._file_catalog import (
    CursorError,
    entries_from_tables,
    page_catalog,
)


def _row(path, *, size=1, modified="2026-03-01T00:00:00Z", created="2026-01-01T00:00:00Z", key="sharepoint_onprem"):
    return SimpleNamespace(name=path, metadata_json={key: {
        "file_id": "/sites/x/Shared Documents/" + path, "size": size,
        "modified_at": modified, "created_at": created, "mime_type": None, "web_url": None,
    }})


@pytest.fixture
def entries():
    rows = [
        _row("a.csv", size=5, modified="2026-03-05T00:00:00Z"),
        _row("Finance/q1.xlsx", size=3, modified="2026-02-01T00:00:00Z", created="2025-12-01T00:00:00Z"),
        _row("Finance/2026/q2.xlsx", size=9, modified="2026-04-01T00:00:00Z"),
        _row("Finance/notes.txt", size=0, modified=None, created=None),
        SimpleNamespace(name="ignored", metadata_json={"something_else": {}}),
        SimpleNamespace(name="custom table", metadata_json=None),
    ]
    out = entries_from_tables(rows)
    assert [e["path"] for e in out] == ["a.csv", "Finance/q1.xlsx", "Finance/2026/q2.xlsx", "Finance/notes.txt"]
    return out


def _paths(page):
    return [e["path"] for e in page]


def test_folder_scope_by_relative_path_or_connector_id(entries):
    page, n, _ = page_catalog(entries, scope_key="k", folder_id="Finance", recursive=False, limit=10)
    assert sorted(_paths(page)) == ["Finance/notes.txt", "Finance/q1.xlsx"] and n == 2
    page, n, _ = page_catalog(entries, scope_key="k", folder_id="finance/", recursive=True, limit=10)
    assert n == 3
    page, n, _ = page_catalog(entries, scope_key="k", folder_id="/sites/x/Shared Documents/Finance/2026", limit=10)
    assert _paths(page) == ["Finance/2026/q2.xlsx"]
    assert page_catalog(entries, scope_key="k", folder_id="Nowhere", limit=10)[1] == 0


def test_glob_matches_name_or_path_and_date_bounds_apply(entries):
    assert page_catalog(entries, scope_key="k", name_pattern="*.xlsx", recursive=True, limit=10)[1] == 2
    assert page_catalog(entries, scope_key="k", name_pattern="finance/2026/*", recursive=True, limit=10)[1] == 1
    page, n, _ = page_catalog(entries, scope_key="k", recursive=True, limit=10,
                              modified_after="2026-03-01", modified_before="2026-04-01T00:00:00Z")
    assert _paths(page) == ["a.csv"]
    # A file without the bounded timestamp never matches that bound.
    page, n, _ = page_catalog(entries, scope_key="k", recursive=True, limit=10, created_after="2000-01-01")
    assert "Finance/notes.txt" not in _paths(page) and n == 3
    with pytest.raises(ValueError):
        page_catalog(entries, scope_key="k", limit=10, created_after="2026-02-01", created_before="2026-01-01")


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_sorting_puts_missing_values_last_in_both_directions(entries, order):
    page, _, _ = page_catalog(entries, scope_key="k", recursive=True, limit=10, sort_by="modified_at", sort_order=order)
    assert page[-1]["path"] == "Finance/notes.txt"
    stamps = [e["modified_at"] for e in page[:-1]]
    assert stamps == sorted(stamps, reverse=(order == "desc"))
    page, _, _ = page_catalog(entries, scope_key="k", recursive=True, limit=10, sort_by="size", sort_order=order)
    sizes = [e["size"] for e in page]
    assert sizes == sorted(sizes, reverse=(order == "desc"))


def test_cursor_walks_every_row_once_and_is_bound_to_the_listing(entries):
    seen, cursor, pages = [], None, 0
    while True:
        page, n, cursor = page_catalog(entries, scope_key="k", recursive=True, limit=3, cursor=cursor,
                                       sort_by="size", sort_order="desc")
        seen += _paths(page)
        pages += 1
        if cursor is None:
            break
    assert pages == 2 and len(seen) == 4 == n and len(set(seen)) == 4

    _, _, cursor = page_catalog(entries, scope_key="k", recursive=True, limit=1)
    for changed in ({"recursive": False}, {"name_pattern": "*.csv"}, {"sort_order": "desc"},
                    {"scope_key": "other-catalog-version"}):
        kwargs = {"scope_key": "k", "recursive": True, "limit": 1, "cursor": cursor}
        kwargs.update(changed)
        with pytest.raises(CursorError):
            page_catalog(entries, **kwargs)
    with pytest.raises(CursorError):
        page_catalog(entries, scope_key="k", recursive=True, limit=1, cursor="not-a-token")
    with pytest.raises(ValueError):
        page_catalog(entries, scope_key="k", limit=1, sort_by="colour")
