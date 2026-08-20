"""SharePoint/OneDrive (Graph) reads must support page_range for PDFs, the
same as the S3 and network_dir clients.

Before this fix, GraphDriveClient.read_file accepted (and silently dropped)
a page_range kwarg via **_, even though it already downloads the full file
body before doing anything else — the same shape S3/network_dir use before
slicing out pages with pypdf. The read_file tool's generic fallback then
reported "This connection does not support page_range reads.", which read
as a platform limitation of SharePoint/Graph rather than an unwired code
path.

Run:
    cd backend && python -m pytest tests/unit/test_graph_drive_page_range.py -v
"""
import pytest

from app.data_sources.clients.graph_drive_client import GraphDriveClient
from tests.unit.test_pdf_surrogate_sanitization import build_multipage_pdf

SITE = "contoso.sharepoint.com,site-guid,web-guid"
PAGES = ["UNIQUE-A", "UNIQUE-B", "UNIQUE-C"]


def _client():
    c = GraphDriveClient(
        site_url="https://contoso.sharepoint.com/sites/hr",
        mode="sharepoint", access_token="tok",
    )
    c._site_id = SITE

    def _get(path, **_ignored):
        if path == f"/sites/{SITE}/drive":
            return {"id": "drv-docs", "name": "Documents"}
        if path == "/drives/drv-docs/items/p1":
            return {
                "id": "p1", "name": "handbook.pdf",
                "file": {"mimeType": "application/pdf"},
                "parentReference": {"path": "/drives/drv-docs/root:"},
            }
        raise ValueError(f"Graph {path} → 404 not found")

    c._get = _get
    c._get_bytes = lambda path: build_multipage_pdf(PAGES)
    return c


def test_graph_drive_page_range_read():
    paged = _client().read_file("p1", page_range=(2, 3))
    assert paged["__doc_pages__"] is True
    assert paged["pages_total"] == 3
    assert "UNIQUE-B" in paged["text"] and "UNIQUE-C" in paged["text"]
    assert "UNIQUE-A" not in paged["text"]


def test_graph_drive_page_range_rejected_for_non_pdf():
    c = _client()
    c._get = lambda path, **_ignored: {
        "id": "x1", "name": "notes.txt", "file": {"mimeType": "text/plain"},
        "parentReference": {"path": "/drives/drv-docs/root:"},
    } if path == "/drives/drv-docs/items/x1" else (_ for _ in ()).throw(ValueError(path))
    with pytest.raises(ValueError):
        c.read_file("x1", page_range=(1, 2))
