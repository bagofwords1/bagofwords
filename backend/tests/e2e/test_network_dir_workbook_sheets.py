"""Multi-sheet Excel on a network_dir share, through the real stack.

Reported (Google Drive, same code shape on every file connector): asked
"what sheets does this Excel have?", the agent answered it could not tell —
read_file handed it the first sheet as CSV and dropped the workbook's sheet
list. Here the connection, data source, report, session files and content
cache are all real; only the LLM is absent (the tool is driven as the agent
loop drives it).

The workbook mimics the report: a Hebrew summary sheet first, the data on
later sheets.
"""
from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.file import File
from tests.e2e.test_network_dir_e2e import _load_ctx, _seed

pytestmark = pytest.mark.e2e

BOOK = "planning/gindi_mavat_kiryat_ono.xlsx"
SHEETS = {
    "סיכום": pd.DataFrame({"מדד": ["מצב טעינה", "כיסוי הרשימה", "סה\"כ תוכניות"],
                           "ערך": ["הושלמה", "528 מתוך 528", 528]}),
    "תוכניות": pd.DataFrame({"מספר תוכנית": [f"502-{i:07d}" for i in range(528)],
                             "סטטוס": ["אושרה" if i % 3 else "בהכנה" for i in range(528)]}),
    "גושים וחלקות": pd.DataFrame({"גוש": [6493, 6494], "חלקה": [12, 40]}),
}


def _write_book(root: Path) -> None:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, df in SHEETS.items():
            df.to_excel(xw, sheet_name=name, index=False)
    path = root / BOOK
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.getvalue())


async def _flow(root: Path):
    from app.ai.tools.implementations.read_file import ReadFileTool

    _write_book(root)
    async with async_session_maker() as db:
        org_id, user_id, conn_id, _ds, report_id = await _seed(db, str(root), writable=False)
        ctx = await _load_ctx(db, org_id, user_id, report_id)

        async def read(**kw):
            agen = ReadFileTool().run_stream({"connection_id": str(conn_id), "file_id": BOOK, **kw}, ctx)
            last = None
            async for ev in agen:
                last = ev
            return last.payload

        # 1. The question from the report: a plain read must name every sheet,
        #    in the observation the planner reads.
        first = await read()
        out, obs = first["output"], first["observation"]
        assert out["success"], out.get("error")
        assert out["sheets"] == list(SHEETS)
        assert out["sheet"] == "סיכום"
        assert all(name in obs["summary"] for name in SHEETS)

        # 2. Following up with a sheet name reads that sheet — not a cache hit
        #    of the first one.
        plans = (await read(sheet="תוכניות"))["output"]
        assert plans["success"], plans.get("error")
        assert plans["sheet"] == "תוכניות" and plans["row_count"] == 528
        assert "מספר תוכנית" in plans["csv"]

        # 3. Repeat reads come from the cache and still carry the sheet list.
        again = await read()
        assert again["output"]["sheets"] == list(SHEETS)
        assert "cached" in again["observation"]["summary"]
        again_plans = (await read(sheet="תוכניות"))["output"]
        assert again_plans["row_count"] == 528

        # 4. Each sheet is its own session file for inspect_data/create_data:
        #    reading sheet 2 must not overwrite sheet 1's file.
        assert out["session_file_id"] != plans["session_file_id"]
        assert again["output"]["session_file_id"] == out["session_file_id"]
        assert again_plans["session_file_id"] == plans["session_file_id"]
        rows = (await db.execute(
            select(File).where(File.id.in_([out["session_file_id"], plans["session_file_id"]]))
        )).scalars().all()
        by_id = {str(f.id): Path(f.path).read_text(encoding="utf-8") for f in rows}
        assert "מספר תוכנית" not in by_id[out["session_file_id"]]
        assert "מספר תוכנית" in by_id[plans["session_file_id"]]

        # 5. A wrong name fails with the real names, so the model can retry.
        bad = (await read(sheet="Sheet2"))["output"]
        assert bad["success"] is False
        assert all(name in bad["error"] for name in SHEETS)


def test_network_dir_workbook_exposes_and_reads_every_sheet(tmp_path):
    asyncio.run(_flow(tmp_path / "share"))
