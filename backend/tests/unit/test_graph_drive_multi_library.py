"""A SharePoint connection must be able to span EVERY document library on a site.

Before this, `_resolve_drive_id` returned exactly one drive:
`GET /sites/{id}/drive` (the site's DEFAULT library) when the Document Library
field was blank, or the single library whose `name` matched when it was set.
There was no code path that unioned libraries, so a site whose content is spread
across several libraries surfaced only one of them — reported live as "4 files
instead of thousands", while other (single-library) sites looked fine.

Opting in with `drive_name='*'`:
  - listing/search fan out across every library;
  - paths are prefixed with the library name so they stay unique and globs can
    address one library;
  - ids are qualified `drive|item` so read_file routes back to the owning
    library — a bare item id is ambiguous once several drives are in play.

Blank keeps the historical single-default-library behaviour so existing
connections do not silently change shape.

Run:
    cd backend && python -m pytest tests/unit/test_graph_drive_multi_library.py -v
"""
import pytest

from app.data_sources.clients.graph_drive_client import GRAPH_BASE, GraphDriveClient


SITE = "contoso.sharepoint.com,site-guid,web-guid"

# Two libraries; 'Policies' holds a nested file, mirroring the real shape.
GRAPH = {
    "/sites/contoso.sharepoint.com:/sites/hr": {"id": SITE},
    f"/sites/{SITE}/drive": {"id": "drv-docs", "name": "Documents"},
    f"/sites/{SITE}/drives": {"value": [
        {"id": "drv-docs", "name": "Documents"},
        {"id": "drv-pol", "name": "Policies"},
    ]},
    "/drives/drv-docs/root": {"id": "root-docs"},
    "/drives/drv-pol/root": {"id": "root-pol"},
    "/drives/drv-docs/items/root-docs/children?$top=200": {"value": [
        {"id": "d1", "name": "budget.xlsx", "file": {"mimeType": "x"}, "size": 1},
    ]},
    "/drives/drv-pol/items/root-pol/children?$top=200": {"value": [
        {"id": "p1", "name": "code_of_conduct.pdf", "file": {"mimeType": "y"}, "size": 2},
        {"id": "f1", "name": "2026", "folder": {}},
    ]},
    "/drives/drv-pol/items/f1/children?$top=200": {"value": [
        {"id": "p2", "name": "handbook.pdf", "file": {"mimeType": "y"}, "size": 3},
    ]},
}


def _client(drive_name, **kw):
    c = GraphDriveClient(
        site_url="https://contoso.sharepoint.com/sites/hr",
        drive_name=drive_name, mode="sharepoint", access_token="tok",
        recursive=True, **kw,
    )
    calls = []

    def _get(path, **_ignored):
        # _list_children passes an absolute URL; everything else passes a path.
        key = path[len(GRAPH_BASE):] if path.startswith(GRAPH_BASE) else path
        calls.append(key)
        if key in GRAPH:
            return GRAPH[key]
        raise ValueError(f"Graph {key} → 404 not found")

    c._get = _get
    c._calls = calls
    return c


# --------------------------------------------------------------- listing

def test_all_libraries_lists_every_library():
    files = _client("*").list_files()
    assert sorted(f["name"] for f in files) == [
        "budget.xlsx", "code_of_conduct.pdf", "handbook.pdf",
    ], "every library on the site must be walked"


def test_paths_are_prefixed_with_the_library_name():
    paths = sorted(f["path"] for f in _client("*").list_files())
    assert paths == [
        "Documents/budget.xlsx",
        "Policies/2026/handbook.pdf",
        "Policies/code_of_conduct.pdf",
    ], "two libraries may hold the same filename — paths must stay unique"


def test_ids_are_qualified_so_reads_can_be_routed():
    by_name = {f["name"]: f for f in _client("*").list_files()}
    assert by_name["handbook.pdf"]["id"] == "drv-pol|p2"
    assert by_name["budget.xlsx"]["id"] == "drv-docs|d1"


def test_a_library_that_cannot_be_read_is_skipped_not_fatal():
    c = _client("*")
    real = c._get

    def _get(path, **kw):
        if path == "/drives/drv-docs/root":
            raise ValueError("Graph … → 403 accessDenied")
        return real(path, **kw)

    c._get = _get
    names = [f["name"] for f in c.list_files()]
    assert "code_of_conduct.pdf" in names, "the readable library must still list"
    assert "budget.xlsx" not in names


# ------------------------------------------------- single-library regression

def test_blank_keeps_the_default_library_only():
    c = _client(None)
    files = c.list_files()
    assert [f["name"] for f in files] == ["budget.xlsx"]
    assert f"/sites/{SITE}/drives" not in c._calls, (
        "a blank field must not enumerate libraries — existing connections "
        "keep hitting /sites/{id}/drive"
    )


def test_blank_leaves_paths_and_ids_untouched():
    f = _client(None).list_files()[0]
    assert f["path"] == "budget.xlsx", "no library prefix for single-library connections"
    assert f["id"] == "d1", "ids stay plain so existing catalog rows keep resolving"


def test_named_library_still_selects_exactly_one():
    files = _client("Policies").list_files()
    assert sorted(f["name"] for f in files) == ["code_of_conduct.pdf", "handbook.pdf"]
    assert all(not f["path"].startswith("Policies/") for f in files), (
        "an explicitly named single library needs no prefix"
    )


def test_unknown_library_name_still_errors():
    with pytest.raises(ValueError, match="not found on site"):
        _client("Nope").list_files()


# --------------------------------------------------------------- read routing

def test_qualified_id_routes_to_its_own_library():
    c = _client("*")
    assert c._locate("drv-pol|p2") == ("drv-pol", "p2")
    assert c._locate("drv-docs|d1") == ("drv-docs", "d1")


def test_bare_id_is_probed_across_libraries():
    """The model does not always echo back the qualified id."""
    c = _client("*")
    GRAPH["/drives/drv-pol/items/p2"] = {"id": "p2", "name": "handbook.pdf"}
    try:
        assert c._locate("p2") == ("drv-pol", "p2")
    finally:
        GRAPH.pop("/drives/drv-pol/items/p2", None)


def test_unresolvable_id_names_the_site_not_one_drive():
    c = _client("*")
    with pytest.raises(ValueError, match="not found in any document library"):
        c._locate("nope-id")


# ------------------------------------------------------------- glob scoping

def test_globs_match_the_library_prefixed_path():
    files = _client("*", include_globs="Policies/**").list_files()
    assert sorted(f["name"] for f in files) == ["code_of_conduct.pdf", "handbook.pdf"], (
        "globs must be matched against the same path the listing shows"
    )


def test_scoped_path_used_for_enforcement_carries_the_prefix():
    c = _client("*")
    c._resolve_drives()  # populate the drive→library map
    rel = c._scoped_path("drv-pol", "/drives/drv-pol/root:/2026", "handbook.pdf")
    assert rel == "Policies/2026/handbook.pdf", (
        "read_file enforces globs on this path — it must match the listed one"
    )


def test_scoped_path_unprefixed_for_single_library():
    c = _client(None)
    c._resolve_drives()
    assert c._scoped_path("drv-docs", "/drives/drv-docs/root:/", "budget.xlsx") == "budget.xlsx"
