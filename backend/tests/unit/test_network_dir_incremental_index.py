"""Incremental content indexing + progress/cancel for network_dir.

Before this feature, every `get_schemas()` run re-extracted text from EVERY
file (pypdf over every PDF, pandas over every workbook) even when nothing had
changed — a scheduled reindex of a static archive repaid the full multi-hour
extraction cost every interval, and the run reported no progress and could not
be cancelled. These tests pin the new contract:

  * a file whose size+mtime match its prior catalog row reuses the stored
    keywords/hash (no extraction);
  * changed / new / prior-unindexed files are (re)extracted;
  * `progress_callback` ticks per file and doubles as the cancel checkpoint
    (raising IndexingCancelled aborts the walk);
  * base.aget_schemas forwards `prior_catalog` only to clients that accept it.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.data_sources.clients.network_dir_client import NetworkDirClient
from app.data_sources.clients.progress import IndexingCancelled


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "alpha.txt").write_text("alpha renewal clause budget\n")
    (tmp_path / "docs" / "beta.txt").write_text("beta indemnity headcount\n")
    (tmp_path / "gamma.md").write_text("# gamma\narbitration forecast\n")
    return tmp_path


def _client(root: Path) -> NetworkDirClient:
    return NetworkDirClient(root_path=str(root), index_mode="content")


@pytest.fixture()
def count_extractions(monkeypatch):
    """Count real content-extraction calls without changing behavior."""
    calls: list[str] = []
    original = NetworkDirClient._file_text

    def counting(self, path, max_chars=200_000):
        calls.append(Path(path).name)
        return original(self, path, max_chars)

    monkeypatch.setattr(NetworkDirClient, "_file_text", counting)
    return calls


def _prior(tables) -> dict:
    return {t.name: t.metadata_json for t in tables}


class TestIncrementalIndexing:
    def test_unchanged_files_reuse_prior_keywords(self, tree, count_extractions):
        client = _client(tree)
        run1 = client.get_schemas()
        assert len(count_extractions) == 3

        run2 = client.get_schemas(prior_catalog=_prior(run1))
        # No new extraction — everything matched size+mtime.
        assert len(count_extractions) == 3
        m1 = {t.name: t.metadata_json["network_dir"] for t in run1}
        m2 = {t.name: t.metadata_json["network_dir"] for t in run2}
        assert m1.keys() == m2.keys()
        for name in m1:
            assert m2[name]["keywords"] == m1[name]["keywords"]
            assert m2[name]["content_hash"] == m1[name]["content_hash"]
            assert m2[name]["indexed"] is True
        # Keyword description survives the reuse path (search context uses it).
        by_name = {t.name: t for t in run2}
        assert "alpha" in by_name["docs/alpha.txt"].description
        assert "Keywords:" in by_name["docs/alpha.txt"].description

    def test_changed_file_is_reextracted(self, tree, count_extractions):
        client = _client(tree)
        run1 = client.get_schemas()
        count_extractions.clear()

        target = tree / "docs" / "alpha.txt"
        target.write_text("alpha totally different retention wording\n")
        # Force an mtime change even on coarse-granularity filesystems.
        st = target.stat()
        os.utime(target, (st.st_atime, st.st_mtime + 5))

        run2 = client.get_schemas(prior_catalog=_prior(run1))
        assert count_extractions == ["alpha.txt"]
        meta = {t.name: t.metadata_json["network_dir"] for t in run2}
        assert "retention" in meta["docs/alpha.txt"]["keywords"]
        old = {t.name: t.metadata_json["network_dir"] for t in run1}
        assert meta["docs/alpha.txt"]["content_hash"] != old["docs/alpha.txt"]["content_hash"]

    def test_same_mtime_different_size_is_reextracted(self, tree, count_extractions):
        client = _client(tree)
        run1 = client.get_schemas()
        count_extractions.clear()

        target = tree / "gamma.md"
        st = target.stat()
        target.write_text("# gamma\narbitration forecast plus appended text\n")
        os.utime(target, (st.st_atime, st.st_mtime))  # keep mtime identical

        client.get_schemas(prior_catalog=_prior(run1))
        assert count_extractions == ["gamma.md"]

    def test_new_and_deleted_files(self, tree, count_extractions):
        client = _client(tree)
        run1 = client.get_schemas()
        count_extractions.clear()

        (tree / "docs" / "delta.txt").write_text("delta severance matrix\n")
        (tree / "gamma.md").unlink()

        run2 = client.get_schemas(prior_catalog=_prior(run1))
        assert count_extractions == ["delta.txt"]
        names = {t.name for t in run2}
        assert "docs/delta.txt" in names
        assert "gamma.md" not in names

    def test_prior_row_without_indexed_flag_is_extracted(self, tree, count_extractions):
        """A prior row from a metadata-only run (or a failed extraction) must
        not satisfy the skip check — content still needs to be extracted."""
        meta_client = NetworkDirClient(root_path=str(tree), index_mode="metadata")
        run_meta = meta_client.get_schemas()
        count_extractions.clear()

        client = _client(tree)
        client.get_schemas(prior_catalog=_prior(run_meta))
        assert len(count_extractions) == 3

    def test_malformed_prior_entries_are_ignored(self, tree, count_extractions):
        client = _client(tree)
        prior = {
            "docs/alpha.txt": None,
            "docs/beta.txt": {"network_dir": "not-a-dict"},
            "gamma.md": "garbage",
        }
        tables = client.get_schemas(prior_catalog=prior)
        assert len(count_extractions) == 3
        assert all(t.metadata_json["network_dir"]["indexed"] for t in tables)


class TestProgressAndCancel:
    def test_progress_ticks_per_file(self, tree):
        events = []

        def cb(phase, item, done, total):
            events.append((phase, item, done, total))

        client = _client(tree)
        client.get_schemas(progress_callback=cb)
        assert events[0] == ("indexing files", None, 0, 3)
        item_events = [e for e in events if e[1] is not None]
        assert len(item_events) == 3
        assert events[-1][2] == events[-1][3] == 3

    def test_cancel_via_callback_aborts_walk(self, tree):
        seen = []

        def cb(phase, item, done, total):
            if item is not None:
                seen.append(item)
            if len(seen) >= 2:
                raise IndexingCancelled()

        client = _client(tree)
        with pytest.raises(IndexingCancelled):
            client.get_schemas(progress_callback=cb)
        assert len(seen) == 2  # aborted promptly, not after the full walk

    def test_metadata_mode_still_reports_progress(self, tree):
        events = []
        meta_client = NetworkDirClient(root_path=str(tree), index_mode="metadata")
        meta_client.get_schemas(progress_callback=lambda *a: events.append(a))
        assert events and events[-1][2] == 3


class TestBaseForwarding:
    @pytest.mark.asyncio
    async def test_prior_catalog_forwarded_when_accepted(self, tree):
        client = _client(tree)
        run1 = await client.aget_schemas()
        run2 = await client.aget_schemas(prior_catalog=_prior(run1))
        m1 = {t.name: t.metadata_json["network_dir"]["content_hash"] for t in run1}
        m2 = {t.name: t.metadata_json["network_dir"]["content_hash"] for t in run2}
        assert m1 == m2

    @pytest.mark.asyncio
    async def test_prior_catalog_not_forwarded_to_legacy_clients(self):
        """A client whose get_schemas takes no kwargs must not receive them."""
        from app.data_sources.clients.base import DataSourceClient

        class LegacyClient(DataSourceClient):
            description = "legacy"

            def test_connection(self):
                return {"success": True}

            def get_schemas(self):
                return ["t1"]

            def get_schema(self, table_name):
                return None

            def prompt_schema(self):
                return ""

            def execute_query(self, **kwargs):
                return None

        client = LegacyClient()
        assert await client.aget_schemas(
            progress_callback=lambda *a: None, prior_catalog={"x": {}}
        ) == ["t1"]
