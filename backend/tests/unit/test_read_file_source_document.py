"""read_file must leave behind the ORIGINAL document, not just a derivative.

A page-scoped read used to return no file id at all: both `page_range`
branches returned before `_persist_session_file` / `_finalize`, so the UI had
nothing to open and the model got no handle back. These tests pin the fixed
contract — the read keeps the real .pdf so the card can render it at the page
that was actually read (/api/files/{id}/embed#page=N) — plus the two traps
that make it subtle: the source_ref must not collide with the rendered copy,
and a rasterized document must never be reported as an image.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.tools.implementations import _file_tool_common as _ftc
from app.ai.tools.implementations._file_tool_common import (
    is_picture_name,
    read_source_bytes,
)
from app.data_sources.clients.network_dir_client import NetworkDirClient
from tests.unit.test_pdf_surrogate_sanitization import build_multipage_pdf
from tests.unit.test_read_file_session_files import (
    _mk_file,
    _run_read,
    _runtime_ctx,
    _textless_pdf,
)

PAGES = ["Alpha page UNIQUE-A", "Bravo page UNIQUE-B", "Charlie page UNIQUE-C"]

# Where persist_source_document reaches for the attach helper. read_file.py
# holds its own reference for page images, so patching this one isolates the
# source-document write from everything else the tool persists.
_ATTACH = "app.ai.tools.implementations._file_tool_common.attach_drive_file_to_session"
_RESOLVE = "app.ai.tools.implementations.read_file.resolve_file_client"


async def _read_connector(tmp_path, tool_input, runtime_ctx=None, attach_id="SRC-1"):
    client = NetworkDirClient(root_path=str(tmp_path))
    with patch(_RESOLVE, new=AsyncMock(return_value=(client, None))), \
            patch(_ATTACH, new=AsyncMock(return_value=attach_id)) as attach:
        payload = await _run_read(tool_input, runtime_ctx or {})
    return payload, attach


class TestSourceDocumentIsKept:
    @pytest.mark.asyncio
    async def test_page_range_keeps_the_real_pdf(self, tmp_path):
        """The persisted bytes are the PDF itself — not the extracted text."""
        (tmp_path / "book.pdf").write_bytes(build_multipage_pdf(PAGES))
        payload, attach = await _read_connector(
            tmp_path, {"connection_id": "C1", "file_id": "book.pdf", "page_range": "2"}
        )
        out = payload["output"]
        assert out["success"] is True
        assert out["session_file_id"] == "SRC-1"
        # The page slice still reaches the model.
        assert out["pages_shown"] == "2-2" and out["pages_total"] == 3
        assert "UNIQUE-B" in (payload["observation"].get("details") or "")

        kw = attach.await_args.kwargs
        assert kw["content_bytes"].startswith(b"%PDF")
        assert kw["filename"] == "book.pdf"
        assert kw["mime_type"] == "application/pdf"

    @pytest.mark.asyncio
    async def test_source_ref_cannot_clobber_the_rendered_copy(self, tmp_path):
        """The rendered .txt/.csv row is keyed on a bare file_id and is what the
        model was handed; the source copy must key on something else or the
        attach helper refreshes the wrong row in place."""
        (tmp_path / "book.pdf").write_bytes(build_multipage_pdf(PAGES))
        _, attach = await _read_connector(
            tmp_path, {"connection_id": "C1", "file_id": "book.pdf", "page_range": "2"}
        )
        ref = attach.await_args.kwargs["source_ref"]
        assert ref == "book.pdf#source"
        assert ref != "book.pdf"

    @pytest.mark.asyncio
    async def test_session_pdf_is_not_re_attached(self, tmp_path):
        """A conversation attachment IS the original — echo its id instead of
        minting a duplicate row on every page read."""
        f = _mk_file(tmp_path, "book.pdf", build_multipage_pdf(PAGES), "application/pdf")
        with patch(_ATTACH, new=AsyncMock(return_value="SHOULD-NOT-BE-USED")) as attach:
            payload = await _run_read(
                {"connection_id": "", "file_id": f.id, "page_range": "2"},
                _runtime_ctx([f]),
            )
        assert payload["output"]["session_file_id"] == f.id
        attach.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_oversize_source_is_skipped_but_the_read_succeeds(
        self, tmp_path, monkeypatch
    ):
        """Past the cap the preview copy is dropped — never the read itself."""
        (tmp_path / "book.pdf").write_bytes(build_multipage_pdf(PAGES))
        monkeypatch.setattr(_ftc, "_PREVIEW_MAX_BYTES", 10)
        payload, attach = await _read_connector(
            tmp_path, {"connection_id": "C1", "file_id": "book.pdf", "page_range": "2"}
        )
        out = payload["output"]
        assert out["success"] is True
        assert out.get("session_file_id") is None
        assert out["pages_shown"] == "2-2"
        attach.assert_not_awaited()


class TestRasterizedDocumentsAreNotImages:
    @pytest.mark.asyncio
    async def test_document_id_never_lands_in_image_file_ids(self, tmp_path):
        """A scanned PDF renders to page images for vision, but the file we keep
        is the PDF. Reporting that id under image_file_ids gives the UI a broken
        <img> and the model the wrong type."""
        (tmp_path / "scan.pdf").write_bytes(
            _textless_pdf([(200, 100), (300, 150), (400, 200)])
        )
        model = MagicMock()
        model.supports_vision = True
        payload, _ = await _read_connector(
            tmp_path,
            {"connection_id": "C1", "file_id": "scan.pdf", "page_range": "2"},
            runtime_ctx={"model": model},
        )
        out = payload["output"]
        assert out["content_type"] == "images"
        assert out["session_file_id"] == "SRC-1"
        assert "image_file_ids" not in out
        # Vision still received the rendered pages.
        assert payload["observation"]["images"]
        assert out["pages_shown"] == "2-2"

    @pytest.mark.asyncio
    async def test_whole_file_session_pdf_is_not_reported_as_an_image(self, tmp_path):
        """The direct regression: a scanned PDF attached to the conversation is
        rasterized for vision, and its own File id used to come back as
        image_file_ids — an id whose bytes are a PDF, not a picture."""
        f = _mk_file(
            tmp_path, "scan.pdf",
            _textless_pdf([(200, 100), (300, 150)]), "application/pdf",
        )
        payload = await _run_read(
            {"connection_id": "", "file_id": f.id}, _runtime_ctx([f])
        )
        out = payload["output"]
        assert out["content_type"] == "images"
        assert out["session_file_id"] == f.id
        assert "image_file_ids" not in out
        assert payload["observation"]["images"]

    @pytest.mark.asyncio
    async def test_session_png_still_points_at_itself(self, tmp_path):
        """The flip side: when the source genuinely IS a picture, its own id is
        the right image id — re-attaching the bytes on every look would be
        pure duplication. This is what the guard must NOT break."""
        from tests.unit.test_read_file_session_files import _png_bytes

        f = _mk_file(tmp_path, "shot.png", _png_bytes(64, 32), "image/png")
        payload = await _run_read(
            {"connection_id": "", "file_id": f.id}, _runtime_ctx([f])
        )
        out = payload["output"]
        assert out["content_type"] == "images"
        assert out["image_file_ids"] == [f.id]


class TestReadSourceBytesNormalizesClientShapes:
    @pytest.mark.asyncio
    async def test_tuple_returning_client(self):
        class TupleClient:
            def read_raw_bytes(self, file_id):
                return b"%PDF-1.4", "real.pdf", "application/pdf"

        content, name, mime = await read_source_bytes(TupleClient(), "docs/real.pdf")
        assert (content, name, mime) == (b"%PDF-1.4", "real.pdf", "application/pdf")

    @pytest.mark.asyncio
    async def test_bare_bytes_client_does_not_raise(self):
        """OneNote returns bare bytes. Unpacking that into three names is a
        TypeError, which is what this normalization exists to prevent."""
        class BareBytesClient:
            def read_raw_bytes(self, file_id):
                return b"<html>page</html>"

        content, name, mime = await read_source_bytes(
            BareBytesClient(), "Notebook/Section/Page"
        )
        assert content == b"<html>page</html>"
        assert name == "Page"
        assert mime is None


@pytest.mark.parametrize(
    "name,expected",
    [
        ("scan.PNG", True),
        ("photo.jpeg", True),
        ("report.pdf", False),
        ("deck.pptx", False),
        ("noextension", False),
        (None, False),
    ],
)
def test_is_picture_name(name, expected):
    assert is_picture_name(name) is expected


class TestPreviewContract:
    """`preview` tells the UI what to render. The frontend must never have to
    infer it: the stored session file is often a derivative of the source."""

    @pytest.mark.asyncio
    async def test_page_range_previews_the_pdf_at_that_page(self, tmp_path):
        (tmp_path / "book.pdf").write_bytes(build_multipage_pdf(PAGES))
        payload, _ = await _read_connector(
            tmp_path, {"connection_id": "C1", "file_id": "book.pdf", "page_range": "2-3"}
        )
        pv = payload["output"]["preview"]
        assert pv["kind"] == "pdf"
        assert pv["file_id"] == "SRC-1"
        assert pv["mime"] == "application/pdf"
        assert pv["target_page"] == 2
        assert pv["pages_total"] == 3

    @pytest.mark.asyncio
    async def test_scanned_pdf_previews_the_document_not_the_render(self, tmp_path):
        """The requirement: even when the backend falls back to PNGs so the
        model can see the pages, the USER gets the real document."""
        (tmp_path / "scan.pdf").write_bytes(
            _textless_pdf([(200, 100), (300, 150), (400, 200)])
        )
        model = MagicMock()
        model.supports_vision = True
        payload, _ = await _read_connector(
            tmp_path,
            {"connection_id": "C1", "file_id": "scan.pdf", "page_range": "2"},
            runtime_ctx={"model": model},
        )
        out = payload["output"]
        assert out["content_type"] == "images"       # what the model got
        assert out["preview"]["kind"] == "pdf"       # what the user gets
        assert out["preview"]["target_page"] == 2

    @pytest.mark.asyncio
    async def test_uploaded_pdf_previews_from_its_own_id(self, tmp_path):
        f = _mk_file(tmp_path, "book.pdf", build_multipage_pdf(PAGES), "application/pdf")
        payload = await _run_read(
            {"connection_id": "", "file_id": f.id}, _runtime_ctx([f])
        )
        pv = payload["output"]["preview"]
        assert pv["kind"] == "pdf"
        assert pv["file_id"] == f.id
        assert pv["target_page"] == 1

    @pytest.mark.asyncio
    async def test_uploaded_png_previews_as_an_image(self, tmp_path):
        from tests.unit.test_read_file_session_files import _png_bytes

        f = _mk_file(tmp_path, "shot.png", _png_bytes(64, 32), "image/png")
        payload = await _run_read(
            {"connection_id": "", "file_id": f.id}, _runtime_ctx([f])
        )
        pv = payload["output"]["preview"]
        assert pv["kind"] == "image"
        assert pv["file_id"] == f.id
        assert pv["image_file_ids"] == [f.id]

    @pytest.mark.asyncio
    async def test_tabular_read_previews_as_a_table(self, tmp_path):
        f = _mk_file(tmp_path, "sales.csv", b"a,b\n1,2\n3,4\n", "text/csv")
        payload = await _run_read(
            {"connection_id": "", "file_id": f.id}, _runtime_ctx([f])
        )
        pv = payload["output"]["preview"]
        assert pv["kind"] == "table"
        # Rendered from this result's own csv — no file to fetch.
        assert pv["file_id"] is None

    @pytest.mark.asyncio
    async def test_text_read_previews_as_text(self, tmp_path):
        f = _mk_file(tmp_path, "notes.txt", b"hello world", "text/plain")
        payload = await _run_read(
            {"connection_id": "", "file_id": f.id}, _runtime_ctx([f])
        )
        assert payload["output"]["preview"]["kind"] == "text"

    @pytest.mark.asyncio
    async def test_preview_is_not_written_to_the_shared_cache(self, tmp_path):
        """_file_cache is keyed on (connection, file, version) and NOT on report,
        so a File id cached here would be served to a different report."""
        from app.ai.tools.implementations import _file_cache

        (tmp_path / "notes.txt").write_bytes(b"plain text body")
        client = NetworkDirClient(root_path=str(tmp_path))
        with patch(_RESOLVE, new=AsyncMock(return_value=(client, None))), \
                patch(_ATTACH, new=AsyncMock(return_value="SRC-1")), \
                patch.object(_file_cache, "write") as write:
            await _run_read({"connection_id": "C1", "file_id": "notes.txt"}, {})
        if write.call_args:
            assert "preview" not in write.call_args.kwargs["rendered"]
            assert "session_file_id" not in write.call_args.kwargs["rendered"]


class TestSingleFetch:
    """The read and the preview must share ONE download. Re-fetching a 20 MB
    object from S3/SharePoint to render a preview is unacceptable overhead."""

    @staticmethod
    def _remote_like(pdf_bytes, name="book.pdf"):
        """Mimics the s3 / graph_drive contract: one fetch returning extracted
        text that still carries the bytes it came from."""
        from app.data_sources.clients._file_source_common import DocumentText

        class RemoteLikeClient:
            def __init__(self):
                self.reads = 0
                self.raw_reads = 0

            async def aread_file(self, file_id, **kwargs):
                self.reads += 1
                return DocumentText(
                    "Bravo page UNIQUE-B", raw=pdf_bytes,
                    name=name, mime="application/pdf",
                )

            async def afile_version(self, file_id):
                return None

            def read_raw_bytes(self, file_id):
                self.raw_reads += 1
                raise AssertionError(
                    "second download: the bytes were already fetched by aread_file"
                )

        return RemoteLikeClient()

    @pytest.mark.asyncio
    async def test_preview_reuses_the_bytes_from_the_read(self):
        pdf = build_multipage_pdf(PAGES)
        client = self._remote_like(pdf)
        with patch(_RESOLVE, new=AsyncMock(return_value=(client, None))), \
                patch(_ATTACH, new=AsyncMock(return_value="SRC-1")) as attach:
            # An OPAQUE provider id, as SharePoint/Drive hand out — the name has
            # to come from the payload, not from parsing the id.
            payload = await _run_read(
                {"connection_id": "C1", "file_id": "01LZCXOPAQUE9F3"}, {}
            )

        assert client.reads == 1
        assert client.raw_reads == 0

        pv = payload["output"]["preview"]
        assert pv["kind"] == "pdf"
        assert pv["file_id"] == "SRC-1"
        assert pv["target_page"] == 1

        # The bytes stored under #source are the real PDF, not the extracted text.
        source_writes = [
            c for c in attach.await_args_list
            if str(c.kwargs.get("source_ref", "")).endswith("#source")
        ]
        assert len(source_writes) == 1
        assert source_writes[0].kwargs["content_bytes"] == pdf
        assert source_writes[0].kwargs["filename"] == "book.pdf"

    @pytest.mark.asyncio
    async def test_local_source_without_carried_bytes_still_works(self, tmp_path):
        """network_dir extracts straight from disk and carries nothing, so it
        falls back to a re-read — free, because it never left the machine."""
        (tmp_path / "book.pdf").write_bytes(build_multipage_pdf(PAGES))
        payload, attach = await _read_connector(
            tmp_path, {"connection_id": "C1", "file_id": "book.pdf"}
        )
        pv = payload["output"]["preview"]
        assert pv["kind"] == "pdf"
        assert pv["file_id"] == "SRC-1"
