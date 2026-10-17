"""list_files group_by='folder' contract.

Promises: every folder at the requested depth below the scope is returned
with the number of matching files under it, its latest modified and earliest
created time; filters apply to files before grouping; folder pages walk every
folder once under a cursor bound to the folder listing; and folder mode never
reads the source live.

The pure grouping is tested on a synthetic catalog; the tool is driven end to
end (real resolver, real catalog rows in the DB) with HTTP as the boundary.
"""
import uuid
from types import SimpleNamespace

import pytest
import requests

from app.ai.tools.implementations._file_catalog import (
    CursorError,
    entries_from_tables,
    group_catalog_folders,
)


def _row(path, *, modified, created="2025-01-01T00:00:00Z"):
    return SimpleNamespace(name=path, metadata_json={"sharepoint_onprem": {
        "file_id": "/sites/deals/Shared Documents/" + path, "size": 10,
        "modified_at": modified, "created_at": created, "mime_type": None, "web_url": None,
    }})


def _synthetic_catalog(deals=37):
    """`deals` top-level folders; deal i holds i % 4 + 1 files at the top and
    a Contracts/ subfolder with i % 3 files. Plus two loose library files."""
    rows, expected = [], {}
    for i in range(deals):
        deal = f"Deal {i:03}"
        n_top, n_sub = i % 4 + 1, i % 3
        for j in range(n_top):
            rows.append(_row(f"{deal}/memo-{j}.docx", modified=f"2026-0{1 + j % 9}-1{i % 10}T00:00:00Z",
                             created=f"2025-0{1 + (i + j) % 9}-01T00:00:00Z"))
        for j in range(n_sub):
            rows.append(_row(f"{deal}/Contracts/contract-{j}.pdf", modified=f"2026-09-2{j}T00:00:00Z"))
        expected[deal] = n_top + n_sub
    rows += [_row("readme.txt", modified="2026-01-01T00:00:00Z"),
             _row("index.xlsx", modified="2026-01-02T00:00:00Z")]
    return entries_from_tables(rows), expected


@pytest.fixture
def catalog():
    return _synthetic_catalog()


def _group(entries, **kw):
    kw.setdefault("limit", 1000)
    return group_catalog_folders(entries, scope_key="k", **kw)


def test_depth_one_returns_every_top_level_folder_with_counts_and_dates(catalog):
    entries, expected = catalog
    page, stats, cursor = _group(entries)
    assert cursor is None
    assert {f["path"]: f["file_count"] for f in page} == expected
    assert stats["folders"] == len(expected)
    assert stats["files_outside_folders"] == 2
    assert stats["files"] == sum(expected.values()) + 2
    for f in page:
        files = [e for e in entries if e["path"].startswith(f["path"] + "/")]
        assert f["latest_modified_at"] == max(e["modified_at"] for e in files)
        assert f["earliest_created_at"] == min(e["created_at"] for e in files)
        assert 0 < len(f["sample_files"]) <= min(3, len(files))
        assert set(f["sample_files"]) <= {e["name"] for e in files}


def test_depth_two_groups_subfolders_and_counts_shallower_files_outside(catalog):
    entries, _ = catalog
    page, stats, _ = _group(entries, depth=2)
    subs = [e for e in entries if e["path"].count("/") == 2]
    assert {f["path"] for f in page} == {e["path"].rsplit("/", 1)[0] for e in subs}
    assert all(f["path"].endswith("/Contracts") for f in page)
    assert sum(f["file_count"] for f in page) == len(subs)
    assert stats["files_outside_folders"] == len(entries) - len(subs)


def test_folder_id_scopes_grouping_below_that_folder(catalog):
    entries, _ = catalog
    deal = next(e["path"].split("/")[0] for e in entries if e["path"].endswith("contract-1.pdf"))
    for folder_id in (deal, "/sites/deals/Shared Documents/" + deal):  # relative path or connector id
        page, stats, _ = _group(entries, folder_id=folder_id)
        assert [f["path"] for f in page] == [f"{deal}/Contracts"]
        assert page[0]["file_count"] == 2
        assert stats["files_outside_folders"] == len([e for e in entries if e["path"].startswith(deal + "/")]) - 2


def test_filters_apply_to_files_before_grouping(catalog):
    entries, expected = catalog
    page, stats, _ = _group(entries, name_pattern="*.pdf")
    pdfs = {}
    for e in entries:
        if e["name"].endswith(".pdf"):
            pdfs[e["path"].split("/")[0]] = pdfs.get(e["path"].split("/")[0], 0) + 1
    assert {f["path"]: f["file_count"] for f in page} == pdfs
    assert len(pdfs) < len(expected)  # folders with no matching file drop out

    page, _, _ = _group(entries, modified_after="2026-09-01")
    recent = [e for e in entries if e["modified_at"] >= "2026-09-01"]
    assert sum(f["file_count"] for f in page) == len(recent)
    assert all(f["latest_modified_at"] >= "2026-09-01" for f in page)


@pytest.mark.parametrize("sort_by,field", [("modified_at", "latest_modified_at"), ("size", "file_count"),
                                           ("name", "path")])
@pytest.mark.parametrize("order", ["asc", "desc"])
def test_folder_sort_uses_the_matching_aggregate(catalog, sort_by, field, order):
    entries, _ = catalog
    page, _, _ = _group(entries, sort_by=sort_by, sort_order=order)
    values = [f[field] for f in page]
    assert values == sorted(values, reverse=(order == "desc"))


@pytest.mark.parametrize("limit", [1, 7, 36, 37])
def test_cursor_walks_every_folder_once_and_rejects_other_listings(catalog, limit):
    entries, expected = catalog
    seen, cursor = [], None
    while True:
        page, stats, cursor = group_catalog_folders(entries, scope_key="k", limit=limit, cursor=cursor,
                                                    sort_by="size", sort_order="desc")
        assert len(page) <= limit
        seen += [f["path"] for f in page]
        if cursor is None:
            break
    assert sorted(seen) == sorted(expected) and stats["folders"] == len(expected)

    _, _, cursor = group_catalog_folders(entries, scope_key="k", limit=1)
    for changed in ({"depth": 2}, {"name_pattern": "*.pdf"}, {"sort_order": "desc"}, {"scope_key": "v2"}):
        kwargs = {"scope_key": "k", "limit": 1, "cursor": cursor, **changed}
        with pytest.raises(CursorError):
            group_catalog_folders(entries, **kwargs)


# ------------------------------------------------------------ tool end to end


@pytest.fixture
def no_http(monkeypatch):
    """Folder mode must never reach SharePoint; record any attempt.

    The SharePoint Server client talks HTTP through requests.Session."""
    calls = []

    def refuse(method):
        def call(session, url, **kwargs):
            calls.append((method, url))
            raise requests.ConnectionError("synthetic: no network in this test")
        return call

    monkeypatch.setattr(requests.Session, "get", refuse("GET"))
    monkeypatch.setattr(requests.Session, "post", refuse("POST"))
    return calls


async def _run_tool(db, conn, inputs):
    from app.ai.tools.implementations.list_files import ListFilesTool
    ds = SimpleNamespace(id=str(uuid.uuid4()), name="Deals", is_active=True, connections=[conn])
    ctx = {"db": db, "organization": SimpleNamespace(id=conn.organization_id),
           "report": SimpleNamespace(data_sources=[ds])}
    events = [e async for e in ListFilesTool().run_stream({"connection_id": str(conn.id), **inputs}, ctx)]
    return events[-1].payload


async def _seed(db, *, deals, indexed=True):
    from app.models.connection import Connection
    from app.models.connection_table import ConnectionTable
    from app.models.organization import Organization

    org = Organization(name=uuid.uuid4().hex)
    db.add(org)
    await db.flush()
    conn = Connection(name="Deals library", type="sharepoint_onprem", is_active=True, auth_policy="system_only",
                      organization_id=org.id,
                      config={"site_url": "https://portal.example.com/sites/deals", "drive_name": "Documents",
                              "api_mode": "2010"})
    conn.encrypt_credentials({"username": "DOMAIN\\reader", "password": "synthetic"})
    db.add(conn)
    await db.flush()
    expected = {}
    if indexed:
        for i in range(deals):
            deal = f"Deal {i:04}"
            expected[deal] = i % 5 + 1
            for j in range(expected[deal]):
                db.add(ConnectionTable(
                    name=f"{deal}/Sub {j % 2}/file-{j}.pdf", connection_id=conn.id, columns=[], pks=[], fks=[],
                    metadata_json={"sharepoint_onprem": {"file_id": f"/sites/deals/Shared Documents/{deal}/{j}",
                                                         "modified_at": f"2026-0{1 + j}-0{1 + i % 9}T00:00:00Z"}},
                ))
    await db.commit()
    await db.refresh(conn)
    return conn, expected


@pytest.mark.db
@pytest.mark.asyncio
async def test_tool_returns_every_top_level_folder_in_one_call(no_http):
    from app.dependencies import async_session_maker
    async with async_session_maker() as db:
        # More folders than a file page or the file observation (50 rows) holds.
        conn, expected = await _seed(db, deals=260)
        payload = await _run_tool(db, conn, {"group_by": "folder"})
    out, obs = payload["output"], payload["observation"]
    assert out["success"] and out["source"] == "catalog" and out["group_by"] == "folder"
    assert out["next_cursor"] is None and out["folder_count"] == len(expected)
    assert {f["path"]: f["file_count"] for f in out["folders"]} == expected
    # The planner reads the observation: every folder is on its own line there.
    lines = obs["details"].splitlines()
    assert len(lines) == len(expected)
    assert all(any(line.startswith(deal + " ") for line in lines) for deal in expected)
    assert str(len(expected)) in obs["summary"]
    assert no_http == []


@pytest.mark.db
@pytest.mark.asyncio
async def test_tool_pages_folders_with_cursor(no_http):
    from app.dependencies import async_session_maker
    async with async_session_maker() as db:
        conn, expected = await _seed(db, deals=23)
        seen, cursor = [], None
        while True:
            out = (await _run_tool(db, conn, {"group_by": "folder", "limit": 5,
                                              **({"cursor": cursor} if cursor else {})}))["output"]
            assert out["success"] and len(out["folders"]) <= 5
            seen += [f["path"] for f in out["folders"]]
            cursor = out["next_cursor"]
            if cursor is None:
                break
    assert sorted(seen) == sorted(expected)


@pytest.mark.db
@pytest.mark.asyncio
@pytest.mark.parametrize("indexed,source", [(True, "live"), (False, "auto"), (False, "live")])
async def test_tool_refuses_folder_mode_without_the_catalog(no_http, indexed, source):
    from app.dependencies import async_session_maker
    async with async_session_maker() as db:
        conn, _ = await _seed(db, deals=4, indexed=indexed)
        out = (await _run_tool(db, conn, {"group_by": "folder", "source": source}))["output"]
    assert no_http == []  # never falls back to crawling the library
    assert out["success"] is False and "catalog" in out["error"]


@pytest.mark.db
@pytest.mark.asyncio
async def test_tool_file_mode_is_unchanged_by_folder_support(no_http):
    from app.dependencies import async_session_maker
    async with async_session_maker() as db:
        conn, expected = await _seed(db, deals=3)
        out = (await _run_tool(db, conn, {"recursive": True}))["output"]
    assert out["success"] and out["source"] == "catalog"
    assert out["file_count"] == sum(expected.values()) and out.get("folders") is None
