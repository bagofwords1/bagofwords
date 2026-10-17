"""Email files served by a SharePoint Server 2010 library (SOAP), through the
on-prem connector and the read_file tool.

Only the SharePoint transport is simulated (the LegacyServer fixture from
test_sharepoint_2010_client). The email parsing, the attachment id grammar
and the tool are all real, so this pins that the email contract holds for a
connector whose ids are server-relative URLs and whose raw reads go through
MS-COPYS GetItem — a very different id/transport shape from a directory.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.ai.tools.implementations.read_file import ReadFileTool
from tests.unit import test_sharepoint_2010_client as sp
from tests.unit.test_email_files import (
    BODY_WORD,
    CSV_WORD,
    PDF_WORD,
    STANDARD_ATTACHMENTS,
    _eml,
    _msg,
)
from tests.unit.test_sharepoint_2010_client import ROOT, legacy  # noqa: F401


async def _read(client, file_id: str) -> dict:
    with patch("app.ai.tools.implementations.read_file.resolve_file_client",
               new=AsyncMock(return_value=(client, None))):
        events = [e async for e in ReadFileTool().run_stream(
            {"connection_id": "C1", "file_id": file_id}, {})]
    return events[-1].payload["output"]


@pytest.mark.parametrize("ext", ["eml", "msg"])
def test_library_email_reads_and_indexes_with_attachment_text(legacy, ext):  # noqa: F811
    raw = _eml(attachments=STANDARD_ATTACHMENTS) if ext == "eml" else _msg()
    legacy.blobs[f"{ROOT}/mail/{BODY_WORD}.{ext}"] = raw
    c = sp.client(drive_name="Documents", recursive=True, index_mode="content")

    text = c.read_file(f"mail/{BODY_WORD}.{ext}")
    assert isinstance(text, str) and "alice@example.com" in text and "#att:0:report.pdf" in text

    rows = {t.name: t.metadata_json["sharepoint_onprem"] for t in c.get_schemas()}
    meta = rows[f"mail/{BODY_WORD}.{ext}"]
    assert meta["indexed"] is True
    # Body AND attached-PDF terms are keywords of the email's catalog row.
    assert {BODY_WORD, PDF_WORD, CSV_WORD} <= set(meta["keywords"])


@pytest.mark.asyncio
@pytest.mark.parametrize("ext", ["eml", "msg"])
async def test_attachments_read_through_the_tool(legacy, ext):  # noqa: F811
    raw = _eml(attachments=STANDARD_ATTACHMENTS) if ext == "eml" else _msg()
    legacy.blobs[f"{ROOT}/mail.{ext}"] = raw
    c = sp.client(drive_name="Documents", recursive=True)

    out = await _read(c, f"mail.{ext}")
    assert out["success"] and f"mail.{ext}#att:1:data.csv" in out["text"]

    out = await _read(c, f"mail.{ext}#att:1:data.csv")
    assert out["success"], out
    assert out["content_type"] == "tabular" and out["row_count"] == 2 and CSV_WORD in out["csv"]

    out = await _read(c, f"mail.{ext}#att:0:report.pdf")
    assert out["success"] and out["content_type"] == "text" and PDF_WORD in out["text"]

    out = await _read(c, f"mail.{ext}#att:5:ghost.bin")
    assert not out["success"] and "#5" in out["error"]


@pytest.mark.asyncio
async def test_attachment_denied_when_library_is_out_of_scope(legacy):  # noqa: F811
    # The email lives in the Policies library; a connection scoped to
    # Documents cannot reach it — nor, therefore, its attachments.
    legacy.blobs[f"{sp.OTHER}/mail.eml"] = _eml(attachments=STANDARD_ATTACHMENTS)
    c = sp.client(drive_name="Documents", recursive=True)
    out = await _read(c, f"{sp.OTHER}/mail.eml#att:1:data.csv")
    assert not out["success"] and CSV_WORD not in out["error"]
    assert "GetItem" not in legacy.calls  # nothing was downloaded
