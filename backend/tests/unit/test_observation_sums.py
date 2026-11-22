"""Plain column sums reach the model through the tool observations only.

``read_query`` (single and multi result) and ``create_data`` put a
``column_sums`` block on the observation's copy of ``data_preview``. The tool
output keeps the plain preview: it goes to the UI, SSE and ``result_json``,
which stay as they are. A hidden preview and a source-flagged ``create_data``
result get no block. The block survives compaction of an older observation and
the transcript's tool-result content.

Seams: ``ReadQueryTool.run_stream`` with a fake DB (the external boundary), the
public ``CreateDataTool.build_success_observation`` (the ``create_data``
success path itself needs code generation), and
``ObservationContextBuilder.add_tool_observation`` and ``build_result_part``
fed a real read_query observation. Pure Python: no DB, no LLM.
"""
from __future__ import annotations

import copy
import json
import logging
from types import SimpleNamespace

import pandas as pd
import pytest

from app.ai.code_execution.code_execution import StreamingCodeExecutor
from app.ai.context.builders.observation_context_builder import ObservationContextBuilder
from app.ai.context.result_parts import build_result_part
from app.ai.data_preview import build_data_preview, clamp_stats, gate_stats_for_privacy
from app.ai.tools.implementations.create_data import CreateDataTool
from app.ai.tools.implementations.read_query import ReadQueryTool
from tests.unit.test_data_preview_sums import _assert_k_clean, _dicts_with_key

# 12 departments. budget_eur and actual_eur are non-negative and k-clean;
# variance_pct is signed (its sum is an accepted, labelled risk); dept_id is
# an identifier and gets no sum.
BUDGET = [100, 120, 130, 150, 160, 170, 180, 200, 210, 220, 240, 260]  # sum 2140
ACTUAL = [95, 125, 128, 155, 150, 175, 190, 195, 205, 230, 235, 270]  # sum 2153
VARIANCE = [-5.0, 4.17, -1.54, 3.33, -6.25, 2.94, 5.56, -2.5, -2.38, 4.55, -2.08, 3.85]  # sum 4.65
EXPECTED_SUMS = {"budget_eur": 2140, "actual_eur": 2153, "variance_pct": 4.65}


def _departments_df():
    df = pd.DataFrame({
        "department": [f"Dept {c}" for c in "ABCDEFGHIJKL"],
        "budget_eur": BUDGET,
        "actual_eur": ACTUAL,
        "variance_pct": VARIANCE,
        "dept_id": list(range(1, 13)),
    })
    _assert_k_clean(df, ["budget_eur", "actual_eur"])
    return df


# 4 regions for the second query of a multi read.
REGION_REVENUE = [100, 250, 400, 610]  # sum 1360; 1360 / 610 = 2.23


def _regions_df():
    df = pd.DataFrame({"region": ["DE", "FR", "IT", "ES"], "revenue": REGION_REVENUE})
    _assert_k_clean(df, ["revenue"])
    return df


def _formatted(df):
    return StreamingCodeExecutor().format_df_for_widget(df)


class _Step:
    def __init__(self, step_id, data, title):
        self.id = step_id
        self.code = "SELECT 1"
        self.title = title
        self.data = data
        self.data_model = {"type": "table"}
        self.view = None


class _Query:
    def __init__(self, query_id, data, title):
        self.id = query_id
        self.title = title
        self.default_step = _Step(f"{query_id}-step", data, title)
        self.steps = [self.default_step]


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def first(self):
        return self._value


class _FakeDB:
    """Queued rows in call order: the Query, then its Visualization."""

    def __init__(self, values):
        self._values = list(values)

    async def execute(self, *args, **kwargs):
        return _Result(self._values.pop(0) if self._values else None)

    async def scalar(self, *args, **kwargs):
        # No owning saved report: the ungated read path.
        return None


class _Settings:
    def __init__(self, allow_llm_see_data):
        self._allow = allow_llm_see_data

    def get_config(self, name):
        return SimpleNamespace(value=self._allow if name == "allow_llm_see_data" else None)


async def _payload(tool_input, rows, settings=None):
    runtime_ctx = {
        "db": _FakeDB(rows),
        "organization": SimpleNamespace(id="org-1"),
        "report": SimpleNamespace(id="report-1"),
        "tool_call_id": "te-read",
    }
    if settings is not None:
        runtime_ctx["settings"] = settings
    events = [e async for e in ReadQueryTool().run_stream(tool_input, runtime_ctx)]
    payload = events[-1].payload
    assert payload["output"]["success"] is True, payload
    return payload


@pytest.fixture
def sums_log(monkeypatch, caplog):
    """INFO records of the data_preview logger (alembic may have disabled it)."""
    monkeypatch.setattr(logging.getLogger("app.ai.data_preview"), "disabled", False)
    caplog.set_level(logging.INFO, logger="app.ai.data_preview")
    return lambda: [r.getMessage() for r in caplog.records if r.name == "app.ai.data_preview"]


@pytest.mark.asyncio
async def test_read_query_single_observation_carries_sums_output_does_not(sums_log):
    formatted = _formatted(_departments_df())
    payload = await _payload({"query_ids": ["q1"]}, [_Query("q1", formatted, "Budget"), None])
    obs, out = payload["observation"], payload["output"]

    block = obs["data_preview"]["column_sums"]
    assert block["note"] == "sum = sum of all 12 rows"
    assert block["sum"] == EXPECTED_SUMS
    assert "dept_id" not in block["sum"]
    # The output (UI, SSE, result_json) keeps the plain preview.
    assert out["results"][0]["data_preview"]["rows"] == obs["data_preview"]["rows"]
    assert _dicts_with_key(out, "column_sums") == []
    # The tool call id reaches the log line; no field name, no value.
    assert sums_log() == ["column sums added: call=te-read rows=12 columns=3"]


@pytest.mark.asyncio
async def test_read_query_multi_entries_carry_sums():
    payload = await _payload(
        {"query_ids": ["q1", "q2"]},
        [_Query("q1", _formatted(_departments_df()), "Budget"), None,
         _Query("q2", _formatted(_regions_df()), "Regions"), None],
    )
    obs, out = payload["observation"], payload["output"]

    entries = obs["results_summary"]
    assert [e["data_preview"]["column_sums"]["sum"] for e in entries] == [
        EXPECTED_SUMS, {"revenue": 1360},
    ]
    assert [e["data_preview"]["column_sums"]["note"] for e in entries] == [
        "sum = sum of all 12 rows", "sum = sum of all 4 rows",
    ]
    assert len(out["results"]) == 2
    assert _dicts_with_key(out, "column_sums") == []


@pytest.mark.asyncio
async def test_read_query_hidden_data_has_no_sums():
    formatted = _formatted(_departments_df())
    hidden = await _payload(
        {"query_ids": ["q1"]}, [_Query("q1", formatted, "Budget"), None], _Settings(False)
    )
    obs = hidden["observation"]
    assert obs["data_preview"]["data_hidden"] is True
    assert _dicts_with_key(hidden, "column_sums") == []
    assert obs["stats"] == clamp_stats(gate_stats_for_privacy(formatted["info"]))

    # Control: the same call with data visible carries the block.
    visible = await _payload(
        {"query_ids": ["q1"]}, [_Query("q1", formatted, "Budget"), None], _Settings(True)
    )
    assert visible["observation"]["data_preview"]["column_sums"]["sum"] == EXPECTED_SUMS


_OBSERVATION_KEYS = ["summary", "data_preview", "stats", "analysis_complete", "final_answer"]


def test_create_data_success_observation_carries_sums(sums_log):
    formatted = _formatted(_departments_df())
    preview = build_data_preview(formatted)

    obs = CreateDataTool.build_success_observation(
        summary="Built a table.", data_preview=preview, data=formatted,
        allow_llm_see_data=True, source_flagged=False, call_id="te-1",
    )

    assert list(obs) == _OBSERVATION_KEYS
    assert obs["summary"] == "Built a table."
    assert obs["data_preview"]["column_sums"]["sum"] == EXPECTED_SUMS
    assert obs["data_preview"]["rows"] == preview["rows"]
    assert obs["stats"] == clamp_stats(formatted["info"])
    assert obs["analysis_complete"] is False and obs["final_answer"] is None
    # The argument is the output's preview: it stays without a block.
    assert "column_sums" not in preview
    assert sums_log() == ["column sums added: call=te-1 rows=12 columns=3"]


def test_create_data_hidden_observation_has_no_sums():
    formatted = _formatted(_departments_df())
    preview = build_data_preview(formatted, allow_llm_see_data=False)

    obs = CreateDataTool.build_success_observation(
        summary="Built a table.", data_preview=preview, data=formatted,
        allow_llm_see_data=False, source_flagged=False, call_id="te-1",
    )

    assert list(obs) == _OBSERVATION_KEYS
    assert obs["data_preview"] is preview and "column_sums" not in preview
    assert obs["stats"] == clamp_stats(gate_stats_for_privacy(formatted["info"]))


def test_create_data_observation_has_no_sums_when_source_flagged():
    formatted = _formatted(_departments_df())
    preview = build_data_preview(formatted)

    flagged = CreateDataTool.build_success_observation(
        summary="Built a table.", data_preview=preview, data=formatted,
        allow_llm_see_data=True, source_flagged=True, call_id="te-1",
    )
    assert flagged["data_preview"] == preview
    assert "column_sums" not in flagged["data_preview"]

    # Control: the same result, not flagged, carries the block.
    clean = CreateDataTool.build_success_observation(
        summary="Built a table.", data_preview=preview, data=formatted,
        allow_llm_see_data=True, source_flagged=False, call_id="te-1",
    )
    assert clean["data_preview"]["column_sums"]["sum"] == EXPECTED_SUMS


async def _read_query_observation():
    formatted = _formatted(_departments_df())
    payload = await _payload({"query_ids": ["q1"]}, [_Query("q1", formatted, "Budget"), None])
    return payload["observation"]


@pytest.mark.asyncio
async def test_compaction_keeps_sums():
    obs = await _read_query_observation()
    before = copy.deepcopy(obs["data_preview"])
    assert len(before["rows"]) == 12

    builder = ObservationContextBuilder()
    builder.add_tool_observation("read_query", {"query_ids": ["q1"]}, obs, loop_index=0)
    builder.add_tool_observation("create_data", {}, {"summary": "ok"}, loop_index=1)

    compacted = builder.tool_observations[0]["observation"]["data_preview"]
    assert len(compacted["rows"]) == 3
    assert compacted["sampled"] is True
    assert compacted["note"] == "sample of 3; 12 rows total"
    assert compacted["note"] != before.get("note")
    # The block keeps its own note: it still covers all 12 rows.
    assert compacted["column_sums"] == {"note": "sum = sum of all 12 rows", "sum": EXPECTED_SUMS}


@pytest.mark.asyncio
async def test_transcript_result_content_carries_sums():
    obs = await _read_query_observation()

    part = build_result_part(call_id="c1", tool_name="read_query", observation=obs)

    content = json.loads(part.content)
    assert content["data_preview"]["column_sums"] == {
        "note": "sum = sum of all 12 rows",
        "sum": EXPECTED_SUMS,
    }
