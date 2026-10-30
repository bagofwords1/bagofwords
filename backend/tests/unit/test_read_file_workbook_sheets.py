"""Excel workbooks on a file source: the model must learn every sheet.

Reported: a user asked "which sheets does this Excel have?" about a .xlsx on
Google Drive and the agent answered that it could not tell — the read handed
it the first sheet as CSV and nothing else. Every file connector read
workbooks with `pd.read_excel(sheet_name=0)` and dropped the sheet list.

The contract, for any connector:
  * a workbook read reports the sheet shown and every sheet in the file, in
    the output AND in the observation the model actually reads;
  * `sheet=<name>` reads that sheet (forgiving case/whitespace);
  * an unknown sheet fails with the real sheet names, not a pandas KeyError;
  * reads of different sheets never share a cache entry or a session file.

Driven through ReadFileTool on a real NetworkDirClient over a mock workbook;
the per-connector reader is checked across the other clients' read paths.
"""
from __future__ import annotations

import io
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from app.ai.tools.implementations import _file_cache
from app.ai.tools.implementations.read_file import ReadFileTool
from app.data_sources.clients._file_source_common import (
    SheetNotFoundError,
    read_excel_sheet,
)
from app.data_sources.clients.network_dir_client import NetworkDirClient

# Hebrew + spaces + a numeric-looking title: the names models get wrong.
SHEETS = {
    "סיכום": pd.DataFrame({"מדד": ["תוכניות", "כיסוי"], "ערך": [528, 1.0]}),
    "Plans 2026": pd.DataFrame({"plan_id": [101, 102, 103], "city": ["Ono", "Ono", "Yehud"]}),
    "2024": pd.DataFrame({"year": [2024], "total": [77]}),
}


def _workbook_bytes(sheets=SHEETS) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name, index=False)
    return buf.getvalue()


@pytest.fixture()
def share(tmp_path, monkeypatch):
    monkeypatch.setattr(_file_cache, "_CACHE_ROOT", tmp_path / "filecache")
    root = tmp_path / "share"
    (root / "reports").mkdir(parents=True)
    (root / "reports" / "plans.xlsx").write_bytes(_workbook_bytes())
    return NetworkDirClient(root_path=str(root))


async def _read(client, **kw):
    tool_input = {"connection_id": "C1", "file_id": "reports/plans.xlsx", **kw}
    with patch(
        "app.ai.tools.implementations.read_file.resolve_file_client",
        new=AsyncMock(return_value=(client, None)),
    ):
        events = [e async for e in ReadFileTool().run_stream(tool_input, {})]
    return events[-1].payload


@pytest.mark.asyncio
async def test_default_read_lists_every_sheet_to_the_model(share):
    payload = await _read(share)
    out, obs = payload["output"], payload["observation"]

    assert out["success"] is True
    assert out["sheets"] == list(SHEETS)
    assert out["sheet"] == list(SHEETS)[0]
    # The observation is all the planner reads — the names must be there.
    for name in SHEETS:
        assert name in obs["summary"]


@pytest.mark.asyncio
@pytest.mark.parametrize("requested,expected", [
    ("Plans 2026", "Plans 2026"),
    ("  plans 2026 ", "Plans 2026"),   # retyped by the model
    ("2024", "2024"),                  # numeric-looking title, not an index
    ("סיכום", "סיכום"),
])
async def test_named_sheet_reads_that_sheet(share, requested, expected):
    payload = await _read(share, sheet=requested)
    out = payload["output"]

    assert out["sheet"] == expected
    assert out["sheets"] == list(SHEETS)
    assert out["row_count"] == len(SHEETS[expected])
    for col in SHEETS[expected].columns:
        assert str(col) in out["csv"]


@pytest.mark.asyncio
async def test_unknown_sheet_fails_with_the_real_names(share):
    payload = await _read(share, sheet="Nope")
    out = payload["output"]

    assert out["success"] is False
    for name in SHEETS:
        assert name in out["error"]


@pytest.mark.asyncio
async def test_each_sheet_read_is_cached_separately(share):
    first = await _read(share)                       # populates the cache
    other = await _read(share, sheet="Plans 2026")   # must NOT be a cache hit of sheet 1
    again = await _read(share)

    assert first["output"]["sheet"] == "סיכום"
    assert other["output"]["sheet"] == "Plans 2026"
    assert "plan_id" in other["output"]["csv"]
    assert again["output"]["sheet"] == "סיכום"
    assert again["output"]["sheets"] == list(SHEETS)  # survives a cache hit


@pytest.mark.asyncio
async def test_each_sheet_gets_its_own_session_file(share):
    calls = []

    async def fake_attach(runtime_ctx, *, filename, content_bytes, mime_type=None,
                          connection_id=None, source_ref=None, **_):
        calls.append((source_ref, filename, content_bytes))
        return f"file-{len(calls)}"

    with patch("app.ai.tools.implementations.read_file.attach_drive_file_to_session",
               new=fake_attach):
        await _read(share)
        await _read(share, sheet="Plans 2026")

    refs = [c[0] for c in calls]
    names = [c[1] for c in calls]
    assert len(set(refs)) == 2, refs        # distinct rows, no overwrite
    assert len(set(names)) == 2, names
    assert b"plan_id" in calls[1][2] and b"plan_id" not in calls[0][2]


# --- shared reader: same contract whichever connector calls it -------------

@pytest.mark.parametrize("trim", [True, False])
def test_reader_tags_sheets_in_both_header_modes(trim):
    df = read_excel_sheet(_workbook_bytes(), "plans 2026", trim=trim)
    assert df.attrs["sheet_names"] == list(SHEETS)
    assert df.attrs["sheet"] == "Plans 2026"
    assert list(df.columns) == ["plan_id", "city"]
    assert len(df) == 3


def test_reader_unknown_sheet_names_the_alternatives():
    with pytest.raises(SheetNotFoundError) as exc:
        read_excel_sheet(_workbook_bytes(), "missing")
    for name in SHEETS:
        assert name in str(exc.value)


# --- each connector's own read path (only the provider boundary is faked) --

def _assert_tagged(df, sheet):
    assert isinstance(df, pd.DataFrame)
    assert df.attrs["sheet_names"] == list(SHEETS)
    assert df.attrs["sheet"] == sheet
    assert len(df) == len(SHEETS[sheet])


@pytest.mark.parametrize("sheet,expected", [(None, "סיכום"), ("2024", "2024")])
def test_sharepoint_online_onedrive_read_tags_sheets(sheet, expected):
    from app.data_sources.clients.graph_drive_client import SharepointClient

    c = SharepointClient(tenant_id="t", client_id="c", client_secret="s",
                         site_url="https://x.sharepoint.com/sites/A")
    with patch.object(c, "_locate", return_value=("drive", "item")), \
         patch.object(c, "_get", return_value={"name": "plans.xlsx"}), \
         patch.object(c, "_enforce_item_scope"), \
         patch.object(c, "_get_bytes", return_value=_workbook_bytes()):
        _assert_tagged(c.read_file("item", sheet=sheet), expected)


@pytest.mark.parametrize("sheet,expected", [(None, "סיכום"), ("Plans 2026", "Plans 2026")])
def test_google_drive_uploaded_xlsx_tags_sheets(sheet, expected):
    from app.data_sources.clients.google_drive_client import GoogleDriveClient

    c = GoogleDriveClient(access_token="tok")
    meta = {"id": "f1", "name": "plans.xlsx",
            "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
    with patch.object(c, "_resolve_file_id", return_value="f1"), \
         patch.object(c, "_get", return_value=meta), \
         patch.object(c, "_get_bytes", return_value=_workbook_bytes()):
        _assert_tagged(c.read_file("f1", sheet=sheet), expected)


def test_google_native_sheet_lists_tabs_on_both_read_paths():
    from app.data_sources.clients.google_drive_client import GOOGLE_SHEET_MIME, GoogleDriveClient

    c = GoogleDriveClient(access_token="tok")
    meta = {"id": "g1", "name": "Plans", "mimeType": GOOGLE_SHEET_MIME}
    tabs = {"sheets": [{"properties": {"title": t}} for t in SHEETS]}
    values = {"values": [["year", "total"], [2024, 77]]}

    def fake_get(url, params=None, **_):
        if url.endswith("/files/g1"):
            return meta
        if "/values/" in url:
            assert url.endswith("/values/2024")
            return values
        return tabs

    with patch.object(c, "_resolve_file_id", return_value="g1"), \
         patch.object(c, "_get", side_effect=fake_get), \
         patch.object(c, "_get_bytes", return_value=_workbook_bytes()):
        _assert_tagged(c.read_file("g1"), "סיכום")            # xlsx export path
        _assert_tagged(c.read_file("g1", sheet="2024"), "2024")  # Sheets API path
        with pytest.raises(SheetNotFoundError):
            c.read_file("g1", sheet="Nope")


def test_s3_read_tags_sheets():
    import boto3
    from botocore.response import StreamingBody
    from botocore.stub import Stubber
    from app.data_sources.clients.s3_client import S3Client

    data = _workbook_bytes()
    client = S3Client(bucket="b", prefix="docs/", region="us-east-1",
                      access_key="AKIATEST", secret_key="secret")
    s3 = boto3.client("s3", region_name="us-east-1",
                      aws_access_key_id="x", aws_secret_access_key="y")
    client._s3 = s3
    stub = Stubber(s3)
    key = {"Bucket": "b", "Key": "docs/plans.xlsx"}
    stub.add_response("head_object", {"ContentLength": len(data)}, key)
    stub.add_response("get_object", {"Body": StreamingBody(io.BytesIO(data), len(data)),
                                     "ContentLength": len(data)}, key)
    with stub:
        _assert_tagged(client.read_file("plans.xlsx", sheet="Plans 2026"), "Plans 2026")
