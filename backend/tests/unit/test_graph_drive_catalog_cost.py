"""What a Microsoft Graph drive costs to test and to index.

The reported symptom was "testing a SharePoint connection takes forever", and
signing in to OneDrive with it. Neither was the connection check itself: both
paths ended up enumerating the whole drive, one serial HTTPS round-trip per
folder, on a connection that was rebuilt (TCP + TLS) for every request.

These tests pin the cost properties that fix, rather than the exact request
shapes:

  - a connection test never walks (see also test_drive_clients.py),
  - a walk reuses ONE pooled HTTP client and lists sibling folders concurrently,
  - `limit` stops a walk early,
  - the catalog honors `index_mode` and `max_catalog_objects`,
  - discovery reports progress, so a background run is observable,
  - the pre-save validation count is bounded and says so.
"""
from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

from app.data_sources.clients.graph_drive_client import (
    GraphDriveClient,
    OnedriveClient,
    SharepointClient,
)


def _fake_tree(fanout: int, depth: int) -> dict:
    """A drive shaped like a personal OneDrive: folders all the way down.

    Returns {folder_id: [child entries]} where every level has `fanout`
    subfolders and one file.
    """
    tree: dict = {}

    def build(folder_id: str, level: int) -> None:
        children = [{
            "id": f"{folder_id}-file",
            "name": f"{folder_id}.csv",
            "file": {"mimeType": "text/csv"},
            "size": 10,
            "lastModifiedDateTime": "2026-01-01T00:00:00Z",
            "webUrl": f"https://x/{folder_id}",
        }]
        if level < depth:
            for i in range(fanout):
                sub = f"{folder_id}.{i}"
                children.append({"id": sub, "name": f"dir{i}", "folder": {"childCount": 2}})
                build(sub, level + 1)
        tree[folder_id] = children

    build("root", 0)
    return tree


def _client(**kwargs) -> OnedriveClient:
    c = OnedriveClient(access_token="user-token", **kwargs)
    c._drive_id = "drive-1"
    c._root_item_id = "root"
    return c


class TestWalkCost:
    def test_every_folder_is_listed_exactly_once(self):
        tree = _fake_tree(fanout=3, depth=3)  # 1 + 3 + 9 + 27 = 40 folders
        calls: list = []
        lock = threading.Lock()

        def fake_children(drive_id, item_id, **_):
            with lock:
                calls.append(item_id)
            return tree[item_id]

        c = _client()
        with patch.object(c, "_list_children", side_effect=fake_children):
            files = c.list_files()

        assert len(calls) == len(tree), "each folder listed once, no repeats"
        assert sorted(calls) == sorted(tree)
        assert len(files) == len(tree), "one file per folder in the fixture"
        # Paths stay root-relative and reflect nesting.
        assert "dir0/root.0.csv" in {f["path"] for f in files}

    def test_siblings_are_listed_concurrently(self):
        """The point of the change: a level's folders overlap in time.

        Each listing blocks on a barrier that only releases once `concurrency`
        threads have arrived — so this deadlocks (and the test times out) if the
        walk is serial.
        """
        tree = _fake_tree(fanout=4, depth=1)  # root + 4 subfolders
        barrier = threading.Barrier(4, timeout=10)
        seen_parallel = threading.Event()

        def fake_children(drive_id, item_id, **_):
            if item_id != "root":
                barrier.wait()
                seen_parallel.set()
            return tree[item_id]

        c = _client(walk_concurrency=4)
        with patch.object(c, "_list_children", side_effect=fake_children):
            files = c.list_files()

        assert seen_parallel.is_set()
        assert len(files) == 5

    def test_walk_concurrency_one_still_works(self):
        """Concurrency is a knob, not a requirement — 1 falls back to the
        original depth-first walk."""
        tree = _fake_tree(fanout=2, depth=2)
        c = _client(walk_concurrency=1)
        with patch.object(c, "_list_children", side_effect=lambda d, i, **_: tree[i]):
            files = c.list_files()
        assert len(files) == len(tree)

    def test_limit_stops_the_walk_early(self):
        tree = _fake_tree(fanout=4, depth=4)  # 341 folders if fully walked
        listed: list = []
        lock = threading.Lock()

        def fake_children(drive_id, item_id, **_):
            with lock:
                listed.append(item_id)
            return tree[item_id]

        c = _client(walk_concurrency=2)
        with patch.object(c, "_list_children", side_effect=fake_children):
            files = c.list_files(limit=5)

        assert len(files) == 5
        assert len(listed) < len(tree), "a bounded listing must not walk everything"

    def test_non_recursive_scope_reads_one_folder(self):
        tree = _fake_tree(fanout=3, depth=2)
        c = SharepointClient(
            site_url="https://x.sharepoint.com/sites/A", access_token="t",
        )
        c._drive_id, c._root_item_id = "drive-1", "root"
        with patch.object(c, "_list_children", side_effect=lambda d, i, **_: tree[i]) as ch:
            files = c.list_files()
        assert ch.call_count == 1, "SharePoint defaults to non-recursive"
        assert len(files) == 1


class TestAllLibrariesCost:
    """`drive_name='*'` (every library on the site) multiplies the walk by the
    library count, so the bounds have to survive the fan-out."""

    def _site(self, libraries: int, **kwargs) -> SharepointClient:
        c = SharepointClient(
            site_url="https://x.sharepoint.com/sites/A",
            drive_name="*",
            access_token="t",
            recursive=True,
            **kwargs,
        )
        c._drives = [(f"drive-{i}", f"Library{i}") for i in range(libraries)]
        c._root_item_ids = {f"drive-{i}": "root" for i in range(libraries)}
        return c

    def test_limit_stops_the_fan_out_across_libraries(self):
        tree = _fake_tree(fanout=3, depth=3)  # 40 files per library
        c = self._site(5, walk_concurrency=2)
        touched: set = set()

        def fake_children(drive_id, item_id, **_):
            touched.add(drive_id)
            return tree[item_id]

        with patch.object(c, "_list_children", side_effect=fake_children):
            files = c.list_files(limit=10)

        assert len(files) == 10
        assert len(touched) < 5, "a bounded listing must stop before every library"
        # Library-prefixed paths and drive-qualified ids still hold.
        assert all(f["path"].startswith("Library") for f in files)
        assert all("|" in f["id"] for f in files)

    def test_connection_test_probes_only_the_primary_library(self):
        """The test must not scale with the library count — reaching one library
        already proves the token, the site and the folder scope."""
        c = self._site(6)
        with patch.object(c, "_token", return_value="t"), \
             patch.object(c, "_resolve_root_item_id", return_value="root") as root, \
             patch.object(c, "_list_children", return_value=[]) as children:
            result = c.test_connection()

        assert result["success"] is True
        assert children.call_count == 1
        assert root.call_count == 1
        assert result["details"]["library_count"] == 6
        assert "6 document libraries" in result["message"]


class TestPooledHttpClient:
    def test_one_client_is_reused_across_requests(self):
        c = _client()
        first = c._client()
        assert c._client() is first
        c.close()
        # After close, a fresh client is created rather than reusing a closed one.
        assert c._client() is not first
        c.close()

    def test_get_goes_through_the_pooled_client(self):
        c = _client()
        with patch.object(c, "_client") as pooled:
            pooled.return_value.get.return_value.status_code = 200
            pooled.return_value.get.return_value.json.return_value = {"value": []}
            c._get("/drives/drive-1/root/children")
        assert pooled.return_value.get.called


class TestCatalogTiers:
    def test_index_mode_none_caches_nothing(self):
        c = _client(index_mode="none")
        with patch.object(c, "_list_children") as children:
            assert c.get_schemas() == []
        assert not children.called, "index_mode=none must not enumerate at all"

    def test_catalog_is_capped(self):
        tree = _fake_tree(fanout=5, depth=3)  # 156 folders → 156 files
        c = _client(max_catalog_objects=10)
        with patch.object(c, "_list_children", side_effect=lambda d, i, **_: tree[i]):
            tables = c.get_schemas()
        assert len(tables) == 10

    def test_default_tier_still_indexes_metadata(self):
        tree = _fake_tree(fanout=2, depth=1)
        c = _client()
        with patch.object(c, "_list_children", side_effect=lambda d, i, **_: tree[i]):
            tables = c.get_schemas()
        assert len(tables) == 3
        meta = tables[0].metadata_json["graph"]
        assert meta["file_id"] and meta["drive_id"] == "drive-1"

    def test_discovery_reports_progress(self):
        """The signal: a background per-user sync must be observable, so both
        the folder walk and the indexing loop emit progress."""
        tree = _fake_tree(fanout=3, depth=2)
        events: list = []

        def cb(phase, item, done, total):
            events.append((phase, item, done, total))

        c = _client()
        with patch.object(c, "_list_children", side_effect=lambda d, i, **_: tree[i]):
            tables = c.get_schemas(progress_callback=cb)

        phases = {e[0] for e in events}
        assert "listing folders" in phases
        assert "indexing files" in phases
        indexing = [e for e in events if e[0] == "indexing files"]
        assert indexing[-1][2] == len(tables) and indexing[-1][3] == len(tables)

    def test_cancellation_propagates_out_of_the_walk(self):
        """The indexing runner cancels by raising from the progress callback;
        that has to unwind the walk instead of being swallowed."""
        from app.data_sources.clients.progress import IndexingCancelled

        tree = _fake_tree(fanout=3, depth=3)

        def cb(phase, item, done, total):
            if done and done > 1:
                raise IndexingCancelled()

        c = _client(walk_concurrency=2)
        with patch.object(c, "_list_children", side_effect=lambda d, i, **_: tree[i]), \
             pytest.raises(IndexingCancelled):
            c.get_schemas(progress_callback=cb)


class TestBoundedValidationCount:
    @pytest.mark.asyncio
    async def test_graph_client_count_is_bounded(self):
        from app.services.connection_service import (
            VALIDATION_FILE_CAP,
            _acount_files_for_validation,
        )

        tree = _fake_tree(fanout=6, depth=4)  # 1555 folders — minutes over the wire
        c = _client()
        with patch.object(c, "_list_children", side_effect=lambda d, i, **_: tree[i]):
            count = await _acount_files_for_validation(c, limit=VALIDATION_FILE_CAP)

        assert count == VALIDATION_FILE_CAP, "count stops at the cap"

    @pytest.mark.asyncio
    async def test_limit_is_not_forced_on_clients_that_reject_it(self):
        """Clients whose `list_files` has no `limit` parameter are called as
        before — the cap must not become a TypeError for them."""
        from app.data_sources.clients.base import Capability
        from app.services.connection_service import _acount_files_for_validation

        class LegacyFileClient:
            capabilities = {Capability.LIST_FILES}

            def list_files(self):
                return [{"id": "1", "name": "a.csv"}, {"id": "2", "name": "b.csv"}]

        assert await _acount_files_for_validation(LegacyFileClient(), limit=50) == 2

    def test_capped_count_is_reported_as_a_floor(self):
        from app.services.connection_service import _connected_message

        exact = _connected_message("sharepoint", 12)
        capped = _connected_message("sharepoint", 200, approximate=True)
        assert "12 files" in exact and "+" not in exact
        assert "200+ files" in capped
        assert "background" in capped, "say where the real count comes from"


class TestGraphClientConfigPlumbing:
    """The knobs have to survive the registry → client construction path
    (these clients take **kwargs, so a typo silently lands in `_ignored`)."""

    @pytest.mark.parametrize("ds_type", ["sharepoint", "onedrive"])
    def test_index_and_cap_reach_the_client(self, ds_type):
        from app.services.connection_service import ConnectionService

        client = ConnectionService()._resolve_client_by_type(
            data_source_type=ds_type,
            config={
                "site_url": "https://x.sharepoint.com/sites/A",
                "index_mode": "none",
                "max_catalog_objects": 42,
            },
            credentials={"tenant_id": "t", "client_id": "c", "client_secret": "s"},
        )
        assert isinstance(client, GraphDriveClient)
        assert client.index_mode == "none"
        assert client.max_catalog_objects == 42

    @pytest.mark.parametrize("ds_type", ["sharepoint", "onedrive"])
    def test_config_schema_exposes_the_knobs(self, ds_type):
        from app.schemas.data_source_registry import get_entry

        fields = get_entry(ds_type).config_schema.model_fields
        assert "index_mode" in fields and "max_catalog_objects" in fields

    def test_outlook_mail_form_is_unchanged(self):
        """Mail keeps no catalog, so the drive knobs must not appear on it."""
        from app.schemas.data_source_registry import get_entry

        fields = get_entry("outlook_mail").config_schema.model_fields
        assert "index_mode" not in fields and "max_catalog_objects" not in fields
