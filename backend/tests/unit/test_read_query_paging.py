"""read_query offset/limit paging: windows are contiguous, trimmed to a byte
budget without skipping rows, and honest about the end."""
from app.ai.tools.implementations.read_query import PAGE_BUDGET_BYTES, PAGE_MAX_CELL_CHARS, build_page
from app.ai.tools.schemas.read_query import ReadQueryInput

import pytest
from pydantic import ValidationError

ROWS = [{"id": i, "name": f"n{i}"} for i in range(250)]


def _page(offset, limit, rows=ROWS, **kw):
    return build_page(rows[offset:offset + limit], ["id", "name"], offset=offset, limit=limit,
                      total_rows=len(rows), source="snapshot", **kw)


def test_pages_walk_the_whole_result_without_gaps():
    seen, offset = [], 0
    while offset is not None:
        p = _page(offset, 100)
        seen += [r["id"] for r in p["rows"]]
        offset = p["next_offset"]
    assert seen == list(range(250))
    assert p["eof"] is True and p["returned"] == 50


def test_offset_past_end_is_empty_eof():
    p = _page(400, 100)
    assert p["returned"] == 0 and p["rows"] == [] and p["eof"] and p["next_offset"] is None


def test_byte_budget_trims_and_next_offset_resumes_at_first_unsent_row():
    wide = [{"id": i, "blob": "x" * (PAGE_MAX_CELL_CHARS * 5)} for i in range(200)]
    p = build_page(wide[:200], ["id", "blob"], offset=0, limit=200, total_rows=200, source="rerun")
    assert 0 < p["returned"] < 200
    assert p["next_offset"] == p["returned"]
    assert all(len(r["blob"]) <= PAGE_MAX_CELL_CHARS + 1 for r in p["rows"])
    assert sum(len(str(r)) for r in p["rows"]) <= PAGE_BUDGET_BYTES * 1.1


def test_hidden_data_reports_shape_only():
    p = _page(0, 100, show_rows=False)
    assert "rows" not in p and p["returned"] == 100 and p["next_offset"] == 100 and "note" in p


def test_input_bounds():
    assert ReadQueryInput(query_ids=["q"], offset=0, limit=500).limit == 500
    with pytest.raises(ValidationError):
        ReadQueryInput(query_ids=["q"], limit=501)
    with pytest.raises(ValidationError):
        ReadQueryInput(query_ids=["q"], offset=-1)


# ── through the tool: snapshot windows, the snapshot-only note, re-execution ──

import pandas as pd

from app.ai.tools.implementations.read_query import ReadQueryTool
from app.ai.tools.schemas.read_query import ReadQueryResult

SNAP = 20
TOTAL = 60


def _result():
    return ReadQueryResult(
        query_id="q1", step_id="s1", title="t",
        data={"columns": [{"field": "i"}], "rows": [{"i": i} for i in range(SNAP)], "info": {"total_rows": TOTAL}},
    )


class _Res:
    def __init__(self, v):
        self.v = v

    def scalar_one(self):
        return self.v


class _DB:
    def __init__(self, query_report_id):
        self.query_report_id = query_report_id

    async def scalar(self, *a, **k):
        return self.query_report_id

    async def execute(self, *a, **k):
        return _Res(_Report())


class _Report:
    id = "report-1"


class _Org:
    id = "org-1"


async def _tool_page(offset, limit, *, query_report_id="report-1", see=True, monkeypatch=None, calls=None):
    from app.services import step_service

    async def _load(self, db, step_id, report=None):
        return object(), None

    async def _params(self, *a, **k):
        return {}

    async def _exec(self, db, step, report, **kw):
        calls.append(kw)
        return pd.DataFrame({"i": range(TOTAL)})

    monkeypatch.setattr(step_service.StepService, "_load_step_for_rerun", _load)
    monkeypatch.setattr(step_service.StepService, "_step_param_specs", lambda self, s: [])
    monkeypatch.setattr(step_service.StepService, "_resolve_step_params", _params)
    monkeypatch.setattr(step_service.StepService, "_execute_step_code", _exec)
    data = ReadQueryInput(query_ids=["q1"], offset=offset, limit=limit)
    r = await ReadQueryTool()._with_page(_DB(query_report_id), {"ds_clients": {"x": 1}}, _Report(), _Org(),
                                         _result(), data, see)
    return r.page


@pytest.mark.asyncio
async def test_window_inside_snapshot_does_not_rerun(monkeypatch):
    calls = []
    p = await _tool_page(5, 10, monkeypatch=monkeypatch, calls=calls)
    assert calls == [] and p["source"] == "snapshot"
    assert [r["i"] for r in p["rows"]] == list(range(5, 15)) and p["next_offset"] == 15 and p["total_rows"] == TOTAL


@pytest.mark.asyncio
async def test_window_past_snapshot_reruns_with_this_runs_clients(monkeypatch):
    calls = []
    p = await _tool_page(15, 30, monkeypatch=monkeypatch, calls=calls)
    assert len(calls) == 1 and calls[0]["return_raw_df"] is True and calls[0]["db_clients"] == {"x": 1}
    assert p["source"] == "re-executed" and [r["i"] for r in p["rows"]] == list(range(15, 45))
    last = await _tool_page(45, 30, monkeypatch=monkeypatch, calls=calls)
    assert [r["i"] for r in last["rows"]] == list(range(45, 60)) and last["eof"] is True


@pytest.mark.asyncio
async def test_other_reports_query_pages_only_its_snapshot(monkeypatch):
    calls = []
    p = await _tool_page(15, 30, query_report_id="report-2", monkeypatch=monkeypatch, calls=calls)
    assert calls == [] and [r["i"] for r in p["rows"]] == list(range(15, 20))
    assert p["eof"] is True and "snapshot" in p["note"]


@pytest.mark.asyncio
async def test_hidden_data_never_returns_rows_even_when_rerun(monkeypatch):
    calls = []
    p = await _tool_page(30, 10, see=False, monkeypatch=monkeypatch, calls=calls)
    assert "rows" not in p and p["returned"] == 10
