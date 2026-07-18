"""Feedback loop — file listings the model can actually use.

Two defects observed live on a Hebrew mortgage-files directory:

1. list_files/search_files observations carry only a COUNT ("Listed 24
   file(s)") — the names/ids exist only in the UI-facing output, so the model
   re-lists blindly until the repeated-call circuit breaker ends the turn
   with a fabricated "Task completed successfully".
2. Filenames stored in a legacy codepage (cp1255 Hebrew) reach Python as
   surrogateescape strings; the persistence sanitizer (correctly) refuses the
   surrogates and every Hebrew character becomes '?' — names are unreadable
   and un-round-trippable.

These tests FAIL on main by design and flip with the fix.
"""
from __future__ import annotations

import json
import os
import uuid as _uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.data_sources.clients.network_dir_client import NetworkDirClient

HEB_NAME_BYTES = "דוח משכנתא 2024.pdf".encode("cp1255")  # legacy-encoded on disk
HEB_NAME = "דוח משכנתא 2024.pdf"
HEB_CONTENT = b"%PDF-fake but greppable MORTGAGE-CODE-77\n"


@pytest.fixture()
def legacy_tree(tmp_path):
    """A dir with one legacy-cp1255-named file, one clean UTF-8 Hebrew name,
    and one ASCII file — created via BYTES paths so the on-disk names really
    are non-UTF-8, exactly like a share written by Windows tools."""
    base = os.fsencode(str(tmp_path))
    with open(os.path.join(base, HEB_NAME_BYTES), "wb") as fh:
        fh.write(HEB_CONTENT)
    (tmp_path / "חוזה.txt").write_text("clean utf8 name\n", encoding="utf-8")
    (tmp_path / "readme.txt").write_text("ascii MORTGAGE-CODE-77\n")
    return tmp_path


def _no_surrogates(s: str) -> bool:
    return not any(0xD800 <= ord(c) <= 0xDFFF for c in s)


# ------------------------------------------------- legacy filename recovery


class TestLegacyFilenameRecovery:
    def test_list_files_recovers_hebrew_names(self, legacy_tree):
        c = NetworkDirClient(root_path=str(legacy_tree))
        entries = c.list_files()
        names = {e["name"] for e in entries}
        assert HEB_NAME in names, f"legacy name not recovered: {names}"
        for e in entries:
            # JSON-safe: no lone surrogates, and no data-destroying '?' runs.
            json.dumps(e, ensure_ascii=False).encode("utf-8")
            assert _no_surrogates(e["name"]) and _no_surrogates(e["path"])
            assert "??" not in e["name"]

    def test_recovered_id_round_trips_to_read(self, legacy_tree):
        c = NetworkDirClient(root_path=str(legacy_tree))
        entry = next(e for e in c.list_files() if e["name"] == HEB_NAME)
        raw = c.read_raw_bytes(entry["id"])[0]
        assert raw == HEB_CONTENT

    def test_recovered_id_round_trips_grep(self, legacy_tree):
        c = NetworkDirClient(root_path=str(legacy_tree))
        sweep = c.grep_files("MORTGAGE-CODE-77", is_regex=False)
        paths = {m["path"] for m in sweep["matches"]}
        assert HEB_NAME in paths  # the legacy-named file is greppable
        assert "readme.txt" in paths
        for p in paths:
            assert _no_surrogates(p) and "??" not in p

    def test_clean_names_unaffected(self, legacy_tree):
        c = NetworkDirClient(root_path=str(legacy_tree))
        names = {e["name"] for e in c.list_files()}
        assert "חוזה.txt" in names and "readme.txt" in names

    def test_file_version_works_for_recovered_id(self, legacy_tree):
        c = NetworkDirClient(root_path=str(legacy_tree))
        entry = next(e for e in c.list_files() if e["name"] == HEB_NAME)
        assert c.file_version(entry["id"])  # resolves + stats without raising


# ------------------------------------------- listing observations (model)


def _runtime_ctx():
    report = MagicMock()
    report.id = "REP-1"
    report.files = []
    report.data_sources = []
    org = MagicMock()
    org.id = "ORG-1"
    org.settings = None
    return {"report": report, "organization": org}


class TestListingObservations:
    @pytest.mark.asyncio
    async def test_list_files_observation_carries_names_and_ids(self, legacy_tree):
        from app.ai.tools.implementations.list_files import ListFilesTool

        c = NetworkDirClient(root_path=str(legacy_tree))
        ds = MagicMock()
        ds.id = "DS1"
        with patch(
            "app.ai.tools.implementations.list_files.resolve_file_data_source",
            new=AsyncMock(return_value=(ds, None)),
        ), patch(
            "app.ai.tools.implementations.list_files.resolve_file_client",
            new=AsyncMock(return_value=(c, None)),
        ):
            tool = ListFilesTool()
            events = [e async for e in tool.run_stream({"connection_id": "C1"}, _runtime_ctx())]
        payload = events[-1].payload
        assert payload["output"]["success"] is True
        details = payload["observation"].get("details") or ""
        # The model must receive the inventory itself — names AND ids.
        assert "readme.txt" in details
        assert HEB_NAME in details

    @pytest.mark.asyncio
    async def test_search_files_observation_carries_names(self):
        from app.ai.tools.implementations.search_files import SearchFilesTool

        client = MagicMock()
        client.asearch_files = AsyncMock(return_value=[
            {"id": "F1", "name": "acme_contract.pdf", "path": "docs/acme_contract.pdf"},
        ])
        with patch(
            "app.ai.tools.implementations.search_files.resolve_file_client",
            new=AsyncMock(return_value=(client, None)),
        ):
            tool = SearchFilesTool()
            events = [e async for e in tool.run_stream(
                {"connection_id": "C1", "query": "acme", "deep": True}, _runtime_ctx(),
            )]
        payload = events[-1].payload
        details = payload["observation"].get("details") or ""
        assert "acme_contract.pdf" in details and "F1" in details

    def test_history_compaction_covers_listing_tools(self):
        import inspect
        from app.ai.context.builders import observation_context_builder as ocb
        src = inspect.getsource(ocb)
        assert "list_files" in src and "search_files" in src, (
            "superseded listing observations must compact their details"
        )


# ------------------------------------------ multi-charset recovery (v2)


CP862_NAME = "תלוש 6 - עצמאי.pdf"           # DOS-Hebrew (zip/SMB era)
CP1252_NAME = "café menu 2024.txt"           # Western — must NOT regress


class TestMultiCharsetRecovery:
    def _write(self, tmp_path, name_bytes: bytes, content: bytes = b"x"):
        with open(os.path.join(os.fsencode(str(tmp_path)), name_bytes), "wb") as fh:
            fh.write(content)

    def test_cp862_names_recover(self, tmp_path):
        self._write(tmp_path, CP862_NAME.encode("cp862"), b"payslip MORTGAGE-CODE-77")
        c = NetworkDirClient(root_path=str(tmp_path))
        names = {e["name"] for e in c.list_files()}
        assert CP862_NAME in names, f"cp862 not recovered: {names}"

    def test_cp862_id_round_trips(self, tmp_path):
        self._write(tmp_path, CP862_NAME.encode("cp862"), b"payslip MORTGAGE-CODE-77")
        c = NetworkDirClient(root_path=str(tmp_path))
        entry = next(e for e in c.list_files() if e["name"] == CP862_NAME)
        assert c.read_raw_bytes(entry["id"])[0] == b"payslip MORTGAGE-CODE-77"

    def test_cp1252_names_not_misdecoded_as_hebrew(self, tmp_path):
        """Scoring guard: cp1255 decodes cp1252 bytes 'successfully' into
        Hebrew glyphs mid-Latin-word; the quality score must prefer cp1252."""
        self._write(tmp_path, CP1252_NAME.encode("cp1252"))
        c = NetworkDirClient(root_path=str(tmp_path))
        names = {e["name"] for e in c.list_files()}
        assert CP1252_NAME in names, f"cp1252 misdecoded: {names}"

    def test_nested_legacy_dirs_round_trip(self, tmp_path):
        base = os.fsencode(str(tmp_path))
        sub = os.path.join(base, "עצמאי".encode("cp862"))
        os.mkdir(sub)
        with open(os.path.join(sub, CP862_NAME.encode("cp862")), "wb") as fh:
            fh.write(b"nested content")
        c = NetworkDirClient(root_path=str(tmp_path))
        entry = next(e for e in c.list_files() if e["name"] == CP862_NAME)
        assert entry["path"] == f"עצמאי/{CP862_NAME}"
        assert c.read_raw_bytes(entry["id"])[0] == b"nested content"

    def test_unknown_encoding_still_round_trips(self, tmp_path, monkeypatch):
        """The encoding-agnostic guarantee: even when NO charset candidate
        matches, an id produced by the listing must resolve back to its file
        via display-form directory matching."""
        import app.data_sources.clients._file_source_common as fsc
        self._write(tmp_path, b"report \x90\x81\x8d final.dat", b"mystery bytes")
        monkeypatch.setattr(fsc, "LEGACY_FILENAME_CHARSETS", ())
        c = NetworkDirClient(root_path=str(tmp_path))
        entries = [e for e in c.list_files() if e["name"].startswith("report")]
        assert len(entries) == 1
        assert c.read_raw_bytes(entries[0]["id"])[0] == b"mystery bytes"

    def test_clean_utf8_hebrew_untouched(self, tmp_path):
        (tmp_path / "חוזה חדש.txt").write_text("clean", encoding="utf-8")
        c = NetworkDirClient(root_path=str(tmp_path))
        names = {e["name"] for e in c.list_files()}
        assert "חוזה חדש.txt" in names


# ----------------------------------------------------- breaker honesty


class TestRepeatedCallBreakerMessage:
    def test_message_does_not_fabricate_success(self):
        from app.ai.agent_v2 import repeated_call_final_answer

        msg = repeated_call_final_answer("list_files", 2)
        assert "list_files" in msg
        assert "success" not in msg.lower()
        assert "achieved" not in msg.lower()
        # It should point the model at the result it already has.
        assert "already" in msg.lower()

    def test_nudge_then_stop_progression(self):
        """2nd identical call → corrective nudge, turn CONTINUES; only the
        3rd+ identical call ends the turn."""
        from app.ai.agent_v2 import repeated_call_action

        assert repeated_call_action(["a:1"], 2) is None
        assert repeated_call_action(["a:1", "b:2"], 2) is None
        assert repeated_call_action(["a:1", "a:1"], 2) == "nudge"
        assert repeated_call_action(["b:2", "a:1", "a:1"], 2) == "nudge"
        assert repeated_call_action(["a:1", "a:1", "a:1"], 2) == "stop"
        # A different call resets the streak.
        assert repeated_call_action(["a:1", "a:1", "b:2"], 2) is None

    def test_nudge_message_is_corrective_not_terminal(self):
        from app.ai.agent_v2 import repeated_call_nudge

        msg = repeated_call_nudge("list_files")
        assert "list_files" in msg
        assert "success" not in msg.lower()
        assert "already" in msg.lower()
