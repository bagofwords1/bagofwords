"""Phase 4: attach-by-link — Drive share URL → file id extraction."""
import pytest

from app.data_sources.clients.google_drive_client import extract_drive_id_from_url

FID = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"


@pytest.mark.parametrize("url", [
    f"https://drive.google.com/file/d/{FID}/view?usp=sharing",
    f"https://docs.google.com/document/d/{FID}/edit",
    f"https://docs.google.com/spreadsheets/d/{FID}/edit#gid=0",
    f"https://docs.google.com/presentation/d/{FID}/edit",
    f"https://drive.google.com/open?id={FID}",
    f"https://drive.google.com/uc?id={FID}&export=download",
])
def test_extracts_file_id_from_drive_urls(url):
    assert extract_drive_id_from_url(url) == FID


def test_extracts_folder_id():
    assert extract_drive_id_from_url(
        f"https://drive.google.com/drive/folders/{FID}"
    ) == FID


@pytest.mark.parametrize("value", [
    "1AbCdEfGhIjKlMnOpQrStUvWxYz012345",   # bare id, not a url
    "Quarterly Report.xlsx",                # filename
    "https://example.com/file/d/abc",       # non-google url
    "",
])
def test_non_drive_urls_return_none(value):
    assert extract_drive_id_from_url(value) is None


def test_resolve_file_id_prefers_url(monkeypatch):
    from app.data_sources.clients.google_drive_client import GoogleDriveClient
    c = GoogleDriveClient(access_token="x")
    # A pasted URL resolves to the embedded id without any name search.
    assert c._resolve_file_id(f"https://drive.google.com/file/d/{FID}/view") == FID
