"""Connection-test schema validation must not content-index file sources.

The pre-save connection tests (test_new_data_source_connection /
test_connection_params) validate "schema access" via _avalidate_schema_access.
For file sources that used to mean get_schemas(), which with the default
'content' index mode extracts text from every PDF/Office document in the
directory — minutes of sequential pypdf work whose output the test discards
after len(). These tests pin the fast path: a metadata-only listing counts the
files, and no document extraction runs at all.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.data_sources.clients.base import Capability
from app.data_sources.clients.network_dir_client import NetworkDirClient
from app.services.connection_service import (
    ConnectionService,
    _acount_files_for_validation,
)
from app.services.data_source_service import DataSourceService


@pytest.fixture()
def pdf_dir(tmp_path: Path) -> Path:
    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4 not a real pdf")
    (tmp_path / "notes.txt").write_text("misc notes\n")
    sub = tmp_path / "decks"
    sub.mkdir()
    (sub / "q3.pdf").write_bytes(b"%PDF-1.4 also fake")
    return tmp_path


@pytest.fixture()
def forbid_extraction(monkeypatch):
    """Any document-text extraction during the connection test is the bug."""
    def _boom(*_a, **_k):
        raise AssertionError("connection test must not extract document content")

    monkeypatch.setattr(
        "app.data_sources.clients.network_dir_client.extract_document_text", _boom
    )


def _client(pdf_dir: Path) -> NetworkDirClient:
    # 'content' is the default index mode — the exact config that made the
    # old path parse every PDF.
    return NetworkDirClient(root_path=str(pdf_dir), index_mode="content")


@pytest.mark.asyncio
async def test_count_helper_lists_without_extraction(pdf_dir, forbid_extraction):
    assert await _acount_files_for_validation(_client(pdf_dir)) == 3


@pytest.mark.asyncio
async def test_count_helper_skips_tabular_clients():
    class FakeSQLClient:
        capabilities = {Capability.QUERY}

    assert await _acount_files_for_validation(FakeSQLClient()) is None


@pytest.mark.asyncio
async def test_count_helper_skips_hybrid_clients():
    """A client with a tabular schema alongside files keeps full validation."""
    class HybridClient:
        capabilities = {Capability.QUERY, Capability.LIST_FILES}

    assert await _acount_files_for_validation(HybridClient()) is None


@pytest.mark.asyncio
async def test_data_source_service_validates_via_listing(pdf_dir, forbid_extraction):
    status = await DataSourceService()._avalidate_schema_access(_client(pdf_dir))
    assert status == {"success": True, "table_count": 3}


@pytest.mark.asyncio
async def test_connection_service_validates_via_listing(pdf_dir, forbid_extraction):
    status = await ConnectionService()._avalidate_schema_access(_client(pdf_dir))
    assert status == {"success": True, "table_count": 3}


@pytest.mark.asyncio
async def test_empty_directory_is_a_valid_file_source(tmp_path):
    """Files can arrive later — an empty-but-readable dir must not fail the
    'no tables found' check meant for databases."""
    status = await ConnectionService()._avalidate_schema_access(
        NetworkDirClient(root_path=str(tmp_path))
    )
    assert status["success"] is True
    assert status["table_count"] == 0
