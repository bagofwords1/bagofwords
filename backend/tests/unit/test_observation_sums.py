"""Plain column sums reach the model through the tool observations only.

``read_query`` (single and multi result) and ``create_data`` put a
``column_sums`` block on the observation's copy of ``data_preview``. The tool
output keeps the plain preview: it goes to the UI, SSE and ``result_json``,
which stay as they are. A hidden preview and a source-flagged ``create_data``
result get no block. The block survives compaction of an older observation and
the transcript's tool-result content.

Seams: ``ReadQueryTool.run_stream`` on the migration-backed test database
(``db``-marked tests: report, owner, org and query come from the API; only the
stored step and its ``default_step_id`` link are seeded directly), the public ``CreateDataTool.build_success_observation``
(the ``create_data`` success path itself needs code generation; pure Python,
no DB), and ``ObservationContextBuilder.add_tool_observation`` and
``build_result_part`` fed a real read_query observation. No LLM.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import uuid
from types import SimpleNamespace

import pandas as pd
import pytest

from app.ai.code_execution.code_execution import StreamingCodeExecutor
from app.ai.context.builders.observation_context_builder import ObservationContextBuilder
from app.ai.context.result_parts import build_result_part
from app.ai.data_preview import build_data_preview, clamp_stats, gate_stats_for_privacy
from app.ai.tools.implementations.create_data import CreateDataTool
from app.ai.tools.implementations.read_query import ReadQueryTool
from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.user import User
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


def _run(coro):
    return asyncio.run(coro)


def api_query_creator(test_client, token, org_id, report_id):
    """``create_query(title)``: POST /api/queries for *report_id* as its owner."""
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    def create_query(title):
        response = test_client.post(
            "/api/queries", json={"title": title, "report_id": report_id}, headers=headers
        )
        assert response.status_code == 200, response.text
        return response.json()

    return create_query


@pytest.fixture
def sums_report(
    test_client, create_report, create_user, login_user, whoami, update_organization_settings
):
    """A report owned by a fresh user and org, created through the API."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    report = create_report(title="Budget", user_token=token, org_id=org_id)
    return SimpleNamespace(
        report_id=report["id"],
        create_query=api_query_creator(test_client, token, org_id, report["id"]),
        hide_data=lambda: update_organization_settings(
            {"allow_llm_see_data": False}, user_token=token, org_id=org_id
        ),
    )


def seed_default_step(report, df, title="Budget by department"):
    """A query of *report* whose default step holds *df*'s stored result.

    The report, its owner and org, and the query with its widget come from
    the API (``report.create_query``). Only the Step and the query's
    ``default_step_id`` link are written directly: a step is written by an
    agent run or a query run (code generation plus execution), and no API
    call stores a step holding a given result.
    """
    query = report.create_query(title)

    async def seed():
        async with async_session_maker() as db:
            step = Step(
                title=title,
                slug=f"sums-s-{uuid.uuid4().hex[:8]}",
                status="success",
                prompt="select department, budget_eur, actual_eur",
                code="select department, budget_eur, actual_eur",
                data=_formatted(df),
                type="table",
                data_model={"type": "table"},
                widget_id=query["widget_id"],
                query_id=query["id"],
            )
            db.add(step)
            await db.flush()
            stored = await db.get(Query, query["id"])
            stored.default_step_id = step.id
            await db.commit()
            return str(step.id)

    step_id = _run(seed())
    return SimpleNamespace(report_id=report.report_id, query_id=query["id"], step_id=step_id)


async def _read(report_id, tool_input):
    """read_query as the agent runs it for the report owner, with the org's settings."""
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        organization = await db.get(Organization, report.organization_id)
        runtime_ctx = {
            "db": db,
            "report": report,
            "organization": organization,
            "user": await db.get(User, report.user_id),
            "settings": await organization.get_settings(db),
            "tool_call_id": "te-read",
        }
        events = [e async for e in ReadQueryTool().run_stream(tool_input, runtime_ctx)]
    payload = events[-1].payload
    assert payload["output"]["success"] is True, payload
    return payload


def _read_departments(sums_report):
    seeded = seed_default_step(sums_report, _departments_df())
    return seeded, _run(_read(sums_report.report_id, {"query_ids": [seeded.query_id]}))


@pytest.fixture
def sums_log(monkeypatch, caplog):
    """INFO records of the data_preview logger (alembic may have disabled it)."""
    monkeypatch.setattr(logging.getLogger("app.ai.data_preview"), "disabled", False)
    caplog.set_level(logging.INFO, logger="app.ai.data_preview")
    return lambda: [r.getMessage() for r in caplog.records if r.name == "app.ai.data_preview"]


@pytest.mark.db
def test_read_query_single_observation_carries_sums_output_does_not(sums_report, sums_log):
    _, payload = _read_departments(sums_report)
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


@pytest.mark.db
def test_read_query_multi_entries_carry_sums(sums_report):
    budget = seed_default_step(sums_report, _departments_df())
    regions = seed_default_step(sums_report, _regions_df(), "Revenue by region")
    payload = _run(_read(sums_report.report_id, {"query_ids": [budget.query_id, regions.query_id]}))
    obs, out = payload["observation"], payload["output"]

    blocks = {e["query_id"]: e["data_preview"]["column_sums"] for e in obs["results_summary"]}
    assert blocks == {
        budget.query_id: {"note": "sum = sum of all 12 rows", "sum": EXPECTED_SUMS},
        regions.query_id: {"note": "sum = sum of all 4 rows", "sum": {"revenue": 1360}},
    }
    assert len(out["results"]) == 2
    assert _dicts_with_key(out, "column_sums") == []


@pytest.mark.db
def test_read_query_hidden_data_has_no_sums(sums_report):
    seeded, visible = _read_departments(sums_report)
    # Control: with data visible the same step carries the block.
    assert visible["observation"]["data_preview"]["column_sums"]["sum"] == EXPECTED_SUMS

    sums_report.hide_data()
    hidden = _run(_read(sums_report.report_id, {"query_ids": [seeded.query_id]}))

    obs = hidden["observation"]
    assert obs["data_preview"]["data_hidden"] is True
    assert _dicts_with_key(hidden, "column_sums") == []
    info = _formatted(_departments_df())["info"]
    assert obs["stats"] == clamp_stats(gate_stats_for_privacy(info))


_OBSERVATION_KEYS = {"summary", "data_preview", "stats", "analysis_complete", "final_answer"}


def test_create_data_success_observation_carries_sums(sums_log):
    formatted = _formatted(_departments_df())
    preview = build_data_preview(formatted)

    obs = CreateDataTool.build_success_observation(
        summary="Built a table.", data_preview=preview, data=formatted,
        allow_llm_see_data=True, source_flagged=False, call_id="te-1",
    )

    assert obs.keys() == _OBSERVATION_KEYS
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

    assert obs.keys() == _OBSERVATION_KEYS
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


@pytest.mark.db
def test_compaction_keeps_sums(sums_report):
    seeded, payload = _read_departments(sums_report)
    obs = payload["observation"]
    before = copy.deepcopy(obs["data_preview"])
    assert len(before["rows"]) == 12

    builder = ObservationContextBuilder()
    builder.add_tool_observation("read_query", {"query_ids": [seeded.query_id]}, obs, loop_index=0)
    builder.add_tool_observation("create_data", {}, {"summary": "ok"}, loop_index=1)

    compacted = builder.tool_observations[0]["observation"]["data_preview"]
    assert len(compacted["rows"]) == 3
    assert compacted["sampled"] is True
    assert compacted["note"] == "sample of 3; 12 rows total"
    assert compacted["note"] != before.get("note")
    # The block keeps its own note: it still covers all 12 rows.
    assert compacted["column_sums"] == {"note": "sum = sum of all 12 rows", "sum": EXPECTED_SUMS}


@pytest.mark.db
def test_transcript_result_content_carries_sums(sums_report):
    _, payload = _read_departments(sums_report)
    obs = payload["observation"]

    part = build_result_part(call_id="c1", tool_name="read_query", observation=obs)

    content = json.loads(part.content)
    assert content["data_preview"]["column_sums"] == {
        "note": "sum = sum of all 12 rows",
        "sum": EXPECTED_SUMS,
    }
