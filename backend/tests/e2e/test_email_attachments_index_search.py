"""Email files through the real stack: a SharePoint 2010 library holding an
.eml with a PDF attachment is content-indexed by the real refresh pipeline
(ConnectionTable → DataSourceTable), search_files finds the email by a term
that exists ONLY inside the attached PDF, and read_file resolves the
attachment id the email's read lists.

Only the SharePoint SOAP transport is simulated.
"""
from __future__ import annotations

import asyncio

import pytest

from app.dependencies import async_session_maker
from tests.e2e.test_sharepoint_2010_index_search import _ctx, _last, _seed
from tests.unit.test_email_files import BODY_WORD, PDF_WORD, STANDARD_ATTACHMENTS, _eml, _msg
from tests.unit.test_sharepoint_2010_client import ROOT, legacy  # noqa: F401


async def _flow(server, ext: str):
    from app.ai.tools.implementations.read_file import ReadFileTool
    from app.ai.tools.implementations.search_files import SearchFilesTool
    from app.services.data_source_service import DataSourceService

    raw = _eml(attachments=STANDARD_ATTACHMENTS) if ext == "eml" else _msg()
    server.blobs[f"{ROOT}/inbox/quarter.{ext}"] = raw
    email_id = f"inbox/quarter.{ext}"

    async with async_session_maker() as db:
        org_id, user_id, conn_id, ds_id, report_id = await _seed(db, "content")
        ctx = await _ctx(db, org_id, user_id, report_id)
        await DataSourceService().refresh_data_source_schema(db, str(ds_id), ctx["organization"], ctx["user"])
        tables = await DataSourceService().get_data_source_schema(
            db=db, data_source_id=str(ds_id), organization=ctx["organization"],
            current_user=ctx["user"], include_inactive=True,
        )
        rows = {t.name: (t.metadata_json or {}).get("sharepoint_onprem", {}) for t in tables}
        assert email_id in rows
        assert {BODY_WORD, PDF_WORD} <= set(rows[email_id].get("keywords") or [])

        # The PDF-only term is answered from the catalog, with no source call.
        calls_before = len(server.calls)
        payload = await _last(SearchFilesTool().run_stream(
            {"connection_id": str(conn_id), "query": PDF_WORD, "max_results": 20}, ctx))
        out = payload["output"]
        assert out["success"], out
        assert [f["path"] for f in out["files"]] == [email_id]
        assert len(server.calls) == calls_before
        found_id = out["files"][0]["id"]

        # Reading the email lists its attachments with ids rooted at the id
        # search handed back; reading one of those ids yields the PDF's text.
        payload = await _last(ReadFileTool().run_stream(
            {"connection_id": str(conn_id), "file_id": found_id}, ctx))
        out = payload["output"]
        assert out["success"] and out["content_type"] == "text", out
        att_id = f"{found_id}#att:0:report.pdf"
        assert att_id in out["text"] and BODY_WORD in out["text"]
        # The model sees the content, not only a summary line.
        assert att_id in payload["observation"].get("details", "")

        payload = await _last(ReadFileTool().run_stream(
            {"connection_id": str(conn_id), "file_id": att_id}, ctx))
        out = payload["output"]
        assert out["success"], out
        assert out["content_type"] == "text" and PDF_WORD in out["text"]
        assert out["file_name"] == "report.pdf"
        # Materialized for the conversation like any other read, as a file
        # of its own (not the email's).
        assert out.get("session_file_id")
        assert out["preview"]["kind"] == "pdf" and out["preview"]["file_id"]

        payload = await _last(ReadFileTool().run_stream(
            {"connection_id": str(conn_id), "file_id": f"{found_id}#att:1:data.csv"}, ctx))
        out = payload["output"]
        assert out["success"] and out["content_type"] == "tabular" and out["row_count"] == 2


@pytest.mark.e2e
@pytest.mark.parametrize("ext", ["eml", "msg"])
def test_email_attachment_is_indexed_searchable_and_readable(legacy, ext):  # noqa: F811
    asyncio.run(_flow(legacy, ext))
