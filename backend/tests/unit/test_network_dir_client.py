"""Unit tests for the NetworkDirClient (network_dir data source).

Exercises the filesystem primitives — list / search (filename + content) /
read / write / copy — plus the security invariants (root confinement, path
traversal rejection, read-only enforcement) against a generated temp tree.
No DB or network.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.data_sources.clients.base import Capability
from app.data_sources.clients.network_dir_client import NetworkDirClient


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """A small deterministic directory tree."""
    (tmp_path / "contracts").mkdir()
    (tmp_path / "images").mkdir()
    (tmp_path / "contracts" / "acme_2025.csv").write_text("clause,penalty\npayment,100\n")
    (tmp_path / "contracts" / "globex_2024.csv").write_text("clause,penalty\nrenewal,50\n")
    (tmp_path / "contracts" / "acme_summary.md").write_text(
        "# Acme Master Agreement\nStatus: active. Vendor Umbrella referenced here.\n"
    )
    (tmp_path / "notes.txt").write_text("misc notes about invoices\n")
    # a tiny binary (fake png header)
    (tmp_path / "images" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    return tmp_path


# ---------------------------------------------------------------- registry


class TestRegistry:
    def test_registered_and_file_shaped(self):
        from app.schemas.data_source_registry import REGISTRY, resolve_client_class

        entry = REGISTRY["network_dir"]
        assert entry.is_connection is True
        assert entry.data_shape == "files"
        assert entry.catalog_ownership == "shared"
        assert resolve_client_class("network_dir") is NetworkDirClient

    def test_type_is_a_file_source(self):
        from app.ai.tools.implementations._file_tool_common import FILE_SOURCE_TYPES

        assert "network_dir" in FILE_SOURCE_TYPES


# ------------------------------------------------------------ capabilities


class TestCapabilities:
    def test_class_advertises_write(self):
        assert Capability.WRITE_FILE in NetworkDirClient.capabilities

    def test_readonly_instance_drops_write(self, tree):
        c = NetworkDirClient(root_path=str(tree), writable=False)
        assert Capability.WRITE_FILE not in c.capabilities
        assert Capability.LIST_FILES in c.capabilities

    def test_writable_instance_keeps_write(self, tree):
        c = NetworkDirClient(root_path=str(tree), writable=True)
        assert Capability.WRITE_FILE in c.capabilities


# --------------------------------------------------------------- reads


class TestReadOnly:
    def test_test_connection(self, tree):
        c = NetworkDirClient(root_path=str(tree))
        res = c.test_connection()
        assert res["success"] is True

    def test_list_files_recursive(self, tree):
        c = NetworkDirClient(root_path=str(tree), recursive=True)
        files = c.list_files()
        ids = {f["id"] for f in files}
        assert "contracts/acme_2025.csv" in ids
        assert "images/logo.png" in ids
        # ids are stable relative POSIX paths
        assert all("\\" not in f["id"] for f in files)

    def test_list_files_non_recursive(self, tree):
        c = NetworkDirClient(root_path=str(tree), recursive=False)
        ids = {f["id"] for f in c.list_files()}
        assert "notes.txt" in ids
        assert "contracts/acme_2025.csv" not in ids  # subfolder excluded

    def test_extension_filter(self, tree):
        c = NetworkDirClient(root_path=str(tree), allowed_extensions="csv")
        exts = {f["name"].rsplit(".", 1)[-1] for f in c.list_files()}
        assert exts == {"csv"}

    def test_read_csv_returns_dataframe(self, tree):
        c = NetworkDirClient(root_path=str(tree))
        df = c.read_file("contracts/acme_2025.csv")
        assert isinstance(df, pd.DataFrame)
        assert list(df.columns) == ["clause", "penalty"]

    def test_read_text(self, tree):
        c = NetworkDirClient(root_path=str(tree))
        text = c.read_file("notes.txt")
        assert isinstance(text, str)
        assert "invoices" in text

    def test_read_binary_returns_bytes(self, tree):
        c = NetworkDirClient(root_path=str(tree))
        b = c.read_file("images/logo.png")
        assert isinstance(b, (bytes, bytearray))
        assert b[:4] == b"\x89PNG"

    def test_search_by_filename(self, tree):
        c = NetworkDirClient(root_path=str(tree))
        hits = c.search_files("acme", content=False)
        ids = {h["id"] for h in hits}
        assert "contracts/acme_2025.csv" in ids
        assert "contracts/globex_2024.csv" not in ids

    def test_search_by_content(self, tree):
        c = NetworkDirClient(root_path=str(tree))
        # "Umbrella" only appears INSIDE acme_summary.md, not in any filename.
        hits = c.search_files("Umbrella")
        ids = {h["id"] for h in hits}
        assert "contracts/acme_summary.md" in ids

    def test_prompt_schema_lists_files(self, tree):
        c = NetworkDirClient(root_path=str(tree))
        text = c.prompt_schema()
        assert "acme_2025.csv" in text


# --------------------------------------------------------------- security


class TestSecurity:
    def test_read_traversal_rejected(self, tree):
        c = NetworkDirClient(root_path=str(tree))
        with pytest.raises(ValueError, match="escapes"):
            c.read_file("../../etc/passwd")

    def test_absolute_escape_rejected(self, tree):
        c = NetworkDirClient(root_path=str(tree), writable=True)
        with pytest.raises(ValueError, match="escapes"):
            c.read_file("/etc/passwd")

    def test_write_on_readonly_rejected(self, tree):
        c = NetworkDirClient(root_path=str(tree), writable=False)
        with pytest.raises(ValueError, match="read-only"):
            c.write_file("x.txt", "hi")

    def test_write_traversal_rejected(self, tree):
        c = NetworkDirClient(root_path=str(tree), writable=True)
        with pytest.raises(ValueError, match="escapes"):
            c.write_file("../escape.txt", "x")

    def test_missing_root_errors(self):
        c = NetworkDirClient(root_path="/no/such/dir/here")
        assert c.test_connection()["success"] is False


# --------------------------------------------------------------- writes


class TestWrites:
    def test_write_text(self, tree):
        c = NetworkDirClient(root_path=str(tree), writable=True)
        entry = c.write_file("out/summary.md", "# hi\n")
        assert entry["id"] == "out/summary.md"
        assert (tree / "out" / "summary.md").read_text() == "# hi\n"

    def test_overwrite_guard(self, tree):
        c = NetworkDirClient(root_path=str(tree), writable=True)
        c.write_file("dup.txt", "a")
        with pytest.raises(ValueError, match="already exists"):
            c.write_file("dup.txt", "b")
        # overwrite=True succeeds
        entry = c.write_file("dup.txt", "b", overwrite=True)
        assert entry["size"] == 1
        assert (tree / "dup.txt").read_text() == "b"

    def test_write_with_folder_arg(self, tree):
        c = NetworkDirClient(root_path=str(tree), writable=True)
        entry = c.write_file("acme.csv", "a,b\n1,2\n", folder_id="related")
        assert entry["id"] == "related/acme.csv"

    def test_write_extension_filter(self, tree):
        c = NetworkDirClient(root_path=str(tree), writable=True, allowed_extensions="csv,md")
        with pytest.raises(ValueError, match="not allowed"):
            c.write_file("bad.exe", "x")

    def test_copy_file_within_root(self, tree):
        c = NetworkDirClient(root_path=str(tree), writable=True)
        entry = c.copy_file("contracts/acme_2025.csv", "backup/acme_copy.csv")
        assert entry["id"] == "backup/acme_copy.csv"
        assert (tree / "backup" / "acme_copy.csv").exists()

    def test_write_bytes(self, tree):
        c = NetworkDirClient(root_path=str(tree), writable=True)
        entry = c.write_file("data/blob.bin", b"\x00\x01\x02")
        assert entry["size"] == 3
