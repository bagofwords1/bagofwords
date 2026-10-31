"""History does not replay data-preview rows (create_data, run_query,
read_query) while the organization hides data from the model
(``allow_llm_see_data`` off), on every report type. Other stored shapes
(read_file text, inspect_data output, describe_entity and write_csv stats,
visualization sample rows) are not covered here yet.

Two leaks are covered:
- read side: the native tool results replayed to the planner come from the
  stored ``context_summary_json``, which may hold rows captured while data was
  still visible;
- write side: a ``create_data`` run with data hidden still carries the full
  ``data`` in its output, and its stored summary took the first row from it.
"""

# Model imports intentionally follow the Alembic registry bootstrap below.
# ruff: noqa: E402
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, update

# Completion blocks and tool executions are internal agent persistence rows
# with no public API to create them, so this test seeds them directly against
# the same model graph Alembic registers.
_env_src = (Path(__file__).resolve().parents[2] / "alembic" / "env.py").read_text()
for _stmt in re.findall(
    r"^from app\.models\S* import \([^)]*\)|^from app\.models[^\n]+",
    _env_src,
    re.M,
):
    exec(_stmt)  # noqa: S102 -- test-only, mirrors alembic/env.py verbatim

from app.ai.context.builders.message_context_builder import (
    MessageContextBuilder,
    _project_create_data_result,
)
from app.ai.context.parts import ToolResultPart
from app.dependencies import async_session_maker
from app.models.agent_execution import AgentExecution
from app.models.completion import Completion
from app.models.completion_block import CompletionBlock
from app.models.organization import Organization
from app.models.report import Report
from app.models.tool_execution import ToolExecution
from app.models.user import User

pytestmark = pytest.mark.db  # opens real sessions — see tests/unit/conftest.py

SSN = "078-05-1120"
REPORT_TYPES = [None, "artifact_chat"]
_COLUMNS = [{"field": "name"}, {"field": "ssn"}]


def _rows(count: int = 3) -> list[dict]:
    return [{"name": f"Person {index}", "ssn": f"{SSN}-{index}"} for index in range(count)]


def _visible_create_data(rows: list[dict]) -> dict:
    return {
        "success": True,
        "data": {"columns": _COLUMNS, "rows": rows},
        "data_preview": {"columns": _COLUMNS, "rows": rows, "row_count": len(rows)},
        "stats": {"total_rows": len(rows)},
    }


def _hidden_create_data(rows: list[dict]) -> dict:
    """create_data's output when data is hidden: the preview has no rows (see
    data_preview.build_data_preview), but the full ``data`` is still there."""
    return {
        "success": True,
        "data": {"columns": _COLUMNS, "rows": rows},
        "data_preview": {
            "columns": _COLUMNS,
            "row_count": len(rows),
            "stats": {},
            "data_hidden": True,
            "note": "Row-level data is hidden by organization policy.",
        },
        "stats": {"total_rows": len(rows)},
    }


def _visible_run_query(rows: list[dict]) -> dict:
    return {
        "success": True,
        "query_id": "query-1",
        "title": "People",
        "applied_params": {"region": "EU"},
        "data_preview": {"columns": _COLUMNS, "rows": rows, "row_count": len(rows)},
    }


def _visible_read_query(rows: list[dict]) -> dict:
    return {
        "success": True,
        "results": [
            {
                "query_id": "query-1",
                "title": "People",
                "data": {"columns": _COLUMNS, "rows": rows},
                "data_preview": {"columns": _COLUMNS, "rows": rows, "row_count": len(rows)},
            }
        ],
    }


@pytest_asyncio.fixture
async def db():
    """The Alembic-backed database selected by the test ``--db`` option."""
    async with async_session_maker() as session:
        yield session
        await session.rollback()


async def _seed(db, *, report_type, tool_name, result_json, legacy=False):
    user = User(name="Viewer", email=f"viewer-{uuid.uuid4()}@example.test", hashed_password="not-used")
    db.add(user)
    await db.flush()
    organization = Organization(name=f"Hidden org {uuid.uuid4()}")
    db.add(organization)
    await db.flush()
    report = Report(
        title="People",
        slug=f"hidden-{uuid.uuid4()}",
        user_id=str(user.id),
        organization_id=str(organization.id),
        report_type=report_type,
    )
    db.add(report)
    await db.flush()
    db.add(
        Completion(
            prompt={"content": "List the people."},
            completion={},
            status="success",
            model="test",
            role="user",
            message_type="ai_completion",
            report_id=str(report.id),
            user_id=str(user.id),
        )
    )
    system = Completion(
        prompt={"content": ""},
        completion={"content": "Here they are."},
        status="success",
        model="test",
        role="system",
        message_type="ai_completion",
        report_id=str(report.id),
    )
    db.add(system)
    await db.flush()
    execution = AgentExecution(
        completion_id=str(system.id),
        organization_id=str(organization.id),
        user_id=str(user.id),
        report_id=str(report.id),
        status="success",
    )
    db.add(execution)
    await db.flush()
    tool = ToolExecution(
        agent_execution_id=str(execution.id),
        tool_name=tool_name,
        tool_action="run",
        arguments_json={},
        status="success",
        success=True,
        result_summary="Done",
        result_json=result_json,
    )
    db.add(tool)
    await db.flush()
    db.add(
        CompletionBlock(
            completion_id=str(system.id),
            agent_execution_id=str(execution.id),
            source_type="tool",
            tool_execution_id=str(tool.id),
            block_index=0,
            title="Data",
            status="completed",
        )
    )
    await db.commit()
    if legacy:
        # A record written before context summaries existed. A bulk update
        # bypasses the model event that snapshots summaries for new writes.
        await db.execute(
            update(ToolExecution).where(ToolExecution.id == str(tool.id)).values(context_summary_json=None)
        )
        await db.commit()
    return SimpleNamespace(
        organization_id=str(organization.id),
        report_id=str(report.id),
        tool_execution_id=str(tool.id),
    )


async def _native(db, scenario, *, allow_llm_see_data: bool) -> list[str]:
    db.expunge_all()
    organization = SimpleNamespace(
        id=scenario.organization_id,
        settings=SimpleNamespace(config={"allow_llm_see_data": {"value": allow_llm_see_data}}),
    )
    report = (await db.execute(select(Report).where(Report.id == scenario.report_id))).scalar_one()
    builder = MessageContextBuilder(db, organization, report)
    builder.use_durable_transcript = True
    await builder.build(max_messages=20)
    contents = [
        part.content
        for turn in builder.get_durable_transcript().turns
        for part in turn.parts
        if isinstance(part, ToolResultPart)
    ]
    assert len(contents) == 1, contents
    return contents


async def _text_history(db, scenario, *, allow_llm_see_data: bool) -> str:
    """History as non-native callers (build_context users, compaction) see it."""
    db.expunge_all()
    organization = SimpleNamespace(
        id=scenario.organization_id,
        settings=SimpleNamespace(config={"allow_llm_see_data": {"value": allow_llm_see_data}}),
    )
    report = (await db.execute(select(Report).where(Report.id == scenario.report_id))).scalar_one()
    return (await MessageContextBuilder(db, organization, report).build(max_messages=20)).render()


def _previews(body: dict) -> list[dict]:
    previews = [body["data_preview"]] if isinstance(body.get("data_preview"), dict) else []
    for item in body.get("results") or []:
        if isinstance(item, dict) and isinstance(item.get("data_preview"), dict):
            previews.append(item["data_preview"])
    return previews


async def _stored_summary(scenario):
    async with async_session_maker() as fresh:
        return (
            await fresh.execute(
                select(ToolExecution.context_summary_json).where(ToolExecution.id == scenario.tool_execution_id)
            )
        ).scalar_one()


# --- read side ---------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("report_type", REPORT_TYPES)
@pytest.mark.parametrize(
    "tool_name,result_json",
    [
        ("create_data", _visible_create_data(_rows())),
        ("run_query", _visible_run_query(_rows())),
        ("read_query", _visible_read_query(_rows())),
    ],
    ids=["create_data", "run_query", "read_query"],
)
async def test_hidden_data_replays_no_row_values(db, report_type, tool_name, result_json):
    """Rows stored while data was visible are not replayed once it is hidden."""
    scenario = await _seed(db, report_type=report_type, tool_name=tool_name, result_json=result_json)
    assert SSN in json.dumps(await _stored_summary(scenario))  # captured while visible

    [content] = await _native(db, scenario, allow_llm_see_data=False)

    assert SSN not in content
    previews = _previews(json.loads(content))
    assert previews
    for preview in previews:
        assert "rows" not in preview
        assert preview["data_hidden"] is True
    assert "query-1" in content or tool_name == "create_data"  # references survive


@pytest.mark.asyncio
@pytest.mark.parametrize("report_type", REPORT_TYPES)
async def test_turning_data_off_after_a_visible_run_query_hides_its_rows(db, report_type):
    scenario = await _seed(
        db, report_type=report_type, tool_name="run_query", result_json=_visible_run_query(_rows(25))
    )

    [visible] = await _native(db, scenario, allow_llm_see_data=True)
    [hidden] = await _native(db, scenario, allow_llm_see_data=False)

    assert SSN in visible
    assert SSN not in hidden
    body = json.loads(hidden)
    assert body["applied_params"] == {"region": "EU"}
    assert body["data_preview"]["row_count"] == 25


# Captured from this exact history at commit 1d49fdb8e (before this change).
_PRE_CHANGE_RUN_QUERY_NATIVE = (
    '{"summary":"Done","version":2,"success":true,"query_id":"query-1","title":"People",'
    '"applied_params":{"region":"EU"},"data_preview":{"columns":[{"field":"name"},{"field":"ssn"}],'
    '"rows":[{"name":"Person 0","ssn":"078-05-1120-0"},{"name":"Person 1","ssn":"078-05-1120-1"}],'
    '"row_count":2}}'
)


@pytest.mark.asyncio
@pytest.mark.parametrize("report_type", REPORT_TYPES)
async def test_visible_data_replay_is_unchanged(db, report_type):
    scenario = await _seed(
        db, report_type=report_type, tool_name="run_query", result_json=_visible_run_query(_rows(2))
    )

    assert await _native(db, scenario, allow_llm_see_data=True) == [_PRE_CHANGE_RUN_QUERY_NATIVE]


# --- write side --------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("report_type", REPORT_TYPES)
async def test_create_data_run_with_data_hidden_persists_no_row(db, report_type):
    scenario = await _seed(
        db, report_type=report_type, tool_name="create_data", result_json=_hidden_create_data(_rows())
    )

    stored = await _stored_summary(scenario)

    assert SSN not in json.dumps(stored)
    assert "rows" not in stored["data_preview"]
    assert stored["data_preview"]["row_count"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("report_type", REPORT_TYPES)
async def test_legacy_create_data_run_with_data_hidden_replays_no_row(db, report_type):
    """A record from before summaries existed: history projects it in SQL and
    writes the projection through. Neither may pick a row out of ``data``,
    even after data is visible again."""
    scenario = await _seed(
        db,
        report_type=report_type,
        tool_name="create_data",
        result_json=_hidden_create_data(_rows()),
        legacy=True,
    )

    rendered = await _text_history(db, scenario, allow_llm_see_data=True)
    [content] = await _native(db, scenario, allow_llm_see_data=True)

    assert "Tool: create_data" in rendered
    assert SSN not in rendered
    assert SSN not in content
    assert SSN not in json.dumps(await _stored_summary(scenario))


def test_decrypted_create_data_projection_keeps_hidden_rows_hidden():
    """Encrypted records are projected in Python; same rule as the SQL path."""
    projection = _project_create_data_result(_hidden_create_data(_rows()))

    assert SSN not in json.dumps(projection)
    assert projection["data_preview"]["row_count"] == 3


def test_decrypted_legacy_create_data_projection_keeps_its_first_row():
    """Records from before data previews existed keep their historical first row."""
    rows = _rows()
    legacy = {"success": True, "data": {"columns": _COLUMNS, "rows": rows}}

    assert _project_create_data_result(legacy)["data_preview"]["rows"] == [rows[0]]
