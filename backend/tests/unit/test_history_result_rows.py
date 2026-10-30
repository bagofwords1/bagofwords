"""A follow-up turn on an artifact_chat thread sees the rows behind its
earlier answer (finding 4, OD-3 A).

The live planner replays earlier tool calls as native tool results built from
``ToolExecution.context_summary_json``, and that summary keeps a single
``create_data`` row. On ``artifact_chat`` threads (the dashboard bubble and app
conversations), and only when the organization lets the model see data,
history carries up to ``HISTORY_RESULT_ROWS_ARTIFACT_CHAT`` whole rows within
``HISTORY_RESULT_ROWS_MAX_CHARS``. Every other report keeps today's history.
"""

# Model imports intentionally follow the Alembic registry bootstrap below.
# ruff: noqa: E402
from __future__ import annotations

import html
import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, select, update

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

from app.ai.context.builders import message_context_builder as mcb
from app.ai.context.builders.message_context_builder import MessageContextBuilder
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


@pytest_asyncio.fixture
async def db():
    """The Alembic-backed database selected by the test ``--db`` option."""
    async with async_session_maker() as session:
        yield session
        await session.rollback()


def _month_rows(count: int) -> list[dict]:
    return [{"month": f"2026-{index:02d}", "revenue": 100 * index} for index in range(1, count + 1)]


def _create_data_result(rows: list[dict], *, row_count: int | None = None) -> dict:
    columns = [{"field": key} for key in rows[0]]
    return {
        "success": True,
        "data": {"columns": columns, "rows": rows},
        "data_preview": {"columns": columns, "rows": rows, "row_count": row_count or len(rows)},
        "stats": {"total_rows": row_count or len(rows)},
    }


async def _seed(db, *, report_type, tool_name="create_data", result_json, legacy=False, title=None):
    user = User(name="Viewer", email=f"viewer-{uuid.uuid4()}@example.test", hashed_password="not-used")
    db.add(user)
    await db.flush()
    organization = Organization(name=f"Rows org {uuid.uuid4()}")
    db.add(organization)
    await db.flush()
    report = Report(
        title="Revenue",
        slug=f"rows-{uuid.uuid4()}",
        user_id=str(user.id),
        organization_id=str(organization.id),
        report_type=report_type,
    )
    db.add(report)
    await db.flush()
    db.add(
        Completion(
            prompt={"content": "Revenue by month?"},
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
        completion={"content": "Total revenue is 1,500."},
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
    if title:
        result_json = {**result_json, "title": title}
    tool = ToolExecution(
        agent_execution_id=str(execution.id),
        tool_name=tool_name,
        tool_action="run",
        arguments_json={},
        status="success",
        success=True,
        result_summary="Built the data",
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
        report_type=report_type,
        tool_execution_id=str(tool.id),
    )


async def _history(db, scenario, *, durable: bool, allow_llm_see_data: bool = True):
    """Build history the way a follow-up turn does; return (xml, native contents)."""
    db.expunge_all()
    organization = SimpleNamespace(
        id=scenario.organization_id,
        settings=SimpleNamespace(config={"allow_llm_see_data": {"value": allow_llm_see_data}}),
    )
    report = (await db.execute(select(Report).where(Report.id == scenario.report_id))).scalar_one()
    builder = MessageContextBuilder(db, organization, report)
    builder.use_durable_transcript = durable
    section = await builder.build(max_messages=20)
    native = [
        part.content
        for turn in builder.get_durable_transcript().turns
        for part in turn.parts
        if isinstance(part, ToolResultPart)
    ]
    return section.render(), native


def _tool_line(rendered: str) -> str:
    lines = [line for line in html.unescape(rendered).splitlines() if "Tool: " in line]
    assert len(lines) == 1, rendered
    return lines[0][lines[0].index("Tool: "):]


def _digest_rows(tool_line: str) -> tuple[int, int, list]:
    match = re.search(r"rows \(first (\d+) of (\d+)\): ", tool_line)
    assert match, tool_line
    rows, _end = json.JSONDecoder().raw_decode(tool_line, match.end())
    return int(match.group(1)), int(match.group(2)), rows


def _rendered_size(rows: list) -> int:
    """Characters the rows take in the prompt (durable_transcript renders
    tool results this way)."""
    return len(json.dumps(rows, ensure_ascii=False, separators=(",", ":")))


def _native_preview_rows(native: list[str]) -> list:
    assert len(native) == 1, native
    return json.loads(native[0])["data_preview"].get("rows") or []


# --- the live path: native tool results -------------------------------------


@pytest.mark.asyncio
async def test_artifact_chat_follow_up_replays_the_earlier_rows(db):
    rows = _month_rows(5)
    scenario = await _seed(db, report_type="artifact_chat", result_json=_create_data_result(rows))

    rendered, native = await _history(db, scenario, durable=True)

    assert "Tool: create_data" not in rendered  # no duplicate text line
    assert _native_preview_rows(native) == rows


@pytest.mark.asyncio
async def test_artifact_chat_rows_reach_calls_recorded_before_summaries(db):
    rows = _month_rows(5)
    scenario = await _seed(db, report_type="artifact_chat", result_json=_create_data_result(rows), legacy=True)

    _rendered, first = await _history(db, scenario, durable=True)
    _rendered, second = await _history(db, scenario, durable=True)

    assert _native_preview_rows(first) == rows
    assert _native_preview_rows(second) == rows


@pytest.mark.asyncio
async def test_enriched_rows_are_never_persisted(db):
    rows = _month_rows(5)
    scenario = await _seed(db, report_type="artifact_chat", result_json=_create_data_result(rows))
    stored_before = (
        await db.execute(
            select(ToolExecution.context_summary_json).where(ToolExecution.id == scenario.tool_execution_id)
        )
    ).scalar_one()

    await _history(db, scenario, durable=True)
    await _history(db, scenario, durable=False)

    async with async_session_maker() as fresh:
        stored_after = (
            await fresh.execute(
                select(ToolExecution.context_summary_json).where(ToolExecution.id == scenario.tool_execution_id)
            )
        ).scalar_one()
    assert stored_after == stored_before
    assert len(stored_after["data_preview"]["rows"]) == 1


@pytest.mark.asyncio
async def test_enrichment_does_not_leak_into_the_reused_builder_cache(db):
    rows = _month_rows(5)
    scenario = await _seed(db, report_type="artifact_chat", result_json=_create_data_result(rows))
    db.expunge_all()
    organization = SimpleNamespace(
        id=scenario.organization_id,
        settings=SimpleNamespace(config={"allow_llm_see_data": {"value": True}}),
    )
    report = (await db.execute(select(Report).where(Report.id == scenario.report_id))).scalar_one()
    builder = MessageContextBuilder(db, organization, report)
    builder.use_durable_transcript = True

    await builder.build(max_messages=20)
    # The org turns data visibility off mid-run: the same builder (and its
    # terminal-execution cache) must stop showing the enriched rows.
    organization.settings.config["allow_llm_see_data"]["value"] = False
    builder.organization_settings = organization.settings
    await builder.build(max_messages=20)
    native = [
        part.content
        for turn in builder.get_durable_transcript().turns
        for part in turn.parts
        if isinstance(part, ToolResultPart)
    ]

    assert len(_native_preview_rows(native)) <= 1


# --- other reports are unchanged ---------------------------------------------

# Captured from this exact history before the change (commit 1d49fdb8e).
_PRE_CHANGE_NATIVE = (
    '{"summary":"Built the data","version":2,"data_preview":{"columns":[{"field":"month"},'
    '{"field":"revenue"}],"row_count":5,"rows":[{"month":"2026-01","revenue":100}]},'
    '"stats":{"total_rows":5},"success":true}'
)
_PRE_CHANGE_DIGEST = (
    'Tool: create_data → run (success) - 5 rows × 2 cols; cols: month, revenue; '
    'top row: {"month": "2026-01", "revenue": 100}'
)


@pytest.mark.asyncio
@pytest.mark.parametrize("report_type", [None, "regular"])
async def test_other_reports_keep_their_history_byte_for_byte(db, report_type):
    scenario = await _seed(db, report_type=report_type, result_json=_create_data_result(_month_rows(5)))

    _rendered, native = await _history(db, scenario, durable=True)
    rendered, _native = await _history(db, scenario, durable=False)

    assert native == [_PRE_CHANGE_NATIVE]
    assert _tool_line(rendered) == _PRE_CHANGE_DIGEST


@pytest.mark.asyncio
async def test_no_rows_when_the_org_hides_data_from_the_model(db):
    scenario = await _seed(db, report_type="artifact_chat", result_json=_create_data_result(_month_rows(5)))

    _rendered, native = await _history(db, scenario, durable=True, allow_llm_see_data=False)
    rendered, _native = await _history(db, scenario, durable=False, allow_llm_see_data=False)

    # Neither the native replay nor the text digest shows a row value.
    assert len(native) == 1
    assert "2026-0" not in native[0]
    preview = json.loads(native[0])["data_preview"]
    assert "rows" not in preview and preview["data_hidden"] is True
    assert preview["row_count"] == 5
    line = _tool_line(rendered)
    assert "rows (first" not in line
    assert "top row" not in line
    assert "2026-01" not in line


# --- caps --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rows_stop_at_ten(db):
    rows = _month_rows(12)
    scenario = await _seed(db, report_type="artifact_chat", result_json=_create_data_result(rows))

    _rendered, native = await _history(db, scenario, durable=True)

    assert mcb.HISTORY_RESULT_ROWS_ARTIFACT_CHAT == 10
    assert _native_preview_rows(native) == rows[:10]


@pytest.mark.asyncio
async def test_wide_rows_stay_within_the_character_budget_as_whole_rows(db):
    # 50 rows of ~300 characters each: about 6 fit in 2,000 characters.
    rows = [{"id": index, "note": f"{index:03d}-" + "x" * 280} for index in range(50)]
    scenario = await _seed(db, report_type="artifact_chat", result_json=_create_data_result(rows, row_count=50))

    _rendered, native = await _history(db, scenario, durable=True)
    rendered, _native = await _history(db, scenario, durable=False)

    native_rows = _native_preview_rows(native)
    assert mcb.HISTORY_RESULT_ROWS_MAX_CHARS == 2000
    assert 1 < len(native_rows) < 10
    assert native_rows == rows[: len(native_rows)]
    assert _rendered_size(native_rows) <= 2000
    assert _rendered_size(rows[: len(native_rows) + 1]) > 2000

    shown, total, digest_rows = _digest_rows(_tool_line(rendered))
    assert total == 50
    assert shown == len(digest_rows) == len(native_rows)
    assert digest_rows == rows[:shown]
    assert _rendered_size(digest_rows) <= 2000


@pytest.mark.asyncio
async def test_a_single_row_over_the_budget_keeps_todays_first_row(db):
    rows = [{"id": index, "note": "y" * 2500} for index in range(3)]
    scenario = await _seed(db, report_type="artifact_chat", result_json=_create_data_result(rows))
    plain = await _seed(db, report_type=None, result_json=_create_data_result(rows))

    _rendered, native = await _history(db, scenario, durable=True)
    _rendered, plain_native = await _history(db, plain, durable=True)

    assert native == plain_native


# --- non-durable callers: the text digest ------------------------------------


@pytest.mark.asyncio
async def test_artifact_chat_digest_lists_all_rows(db):
    rows = _month_rows(5)
    scenario = await _seed(db, report_type="artifact_chat", result_json=_create_data_result(rows))

    rendered, _native = await _history(db, scenario, durable=False)

    line = _tool_line(rendered)
    assert "top row" not in line
    assert _digest_rows(line) == (5, 5, rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["run_query", "describe_entity"])
async def test_query_and_entity_digests_carry_rows_and_keep_params(db, tool_name):
    rows = _month_rows(5)
    result = {**_create_data_result(rows), "applied_params": {"region": "EU"}}
    scenario = await _seed(
        db, report_type="artifact_chat", tool_name=tool_name, result_json=result, title="Monthly revenue"
    )

    rendered, _native = await _history(db, scenario, durable=False)

    line = _tool_line(rendered)
    assert "params: region='EU'" in line
    assert _digest_rows(line) == (5, 5, rows)


@pytest.mark.asyncio
async def test_hebrew_rows_fill_the_budget_by_their_rendered_size(db):
    # ~250 rendered characters per row: 7 fit in 2,000; escaped as \uXXXX they
    # would count about 6x and only one would fit.
    rows = [{"id": index, "לקוח": "שלום עולם " * 24} for index in range(12)]
    scenario = await _seed(db, report_type="artifact_chat", result_json=_create_data_result(rows))

    _rendered, native = await _history(db, scenario, durable=True)
    rendered, _native = await _history(db, scenario, durable=False)

    native_rows = _native_preview_rows(native)
    assert native_rows == rows[: len(native_rows)]
    assert _rendered_size(native_rows) <= 2000
    assert _rendered_size(rows[: len(native_rows) + 1]) > 2000
    line = _tool_line(rendered)
    assert "\\u05" not in line
    assert "שלום עולם" in line
    assert _digest_rows(line)[0] == len(native_rows)


async def _builder(db, scenario, *, durable=True):
    db.expunge_all()
    organization = SimpleNamespace(
        id=scenario.organization_id,
        settings=SimpleNamespace(config={"allow_llm_see_data": {"value": True}}),
    )
    report = (await db.execute(select(Report).where(Report.id == scenario.report_id))).scalar_one()
    builder = MessageContextBuilder(db, organization, report)
    builder.use_durable_transcript = durable
    return builder


def _native_of(builder) -> list[str]:
    return [
        part.content
        for turn in builder.get_durable_transcript().turns
        for part in turn.parts
        if isinstance(part, ToolResultPart)
    ]


@pytest.mark.asyncio
async def test_repeated_builds_reuse_the_rows_of_finished_calls(db):
    """The planner rebuilds history several times per loop iteration; the
    result rows of a finished call are read from the database once."""
    rows = _month_rows(5)
    scenario = await _seed(db, report_type="artifact_chat", result_json=_create_data_result(rows))
    builder = await _builder(db, scenario)
    await builder.build(max_messages=20)

    statements: list[str] = []

    def _record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    event.listen(db.bind.sync_engine, "before_cursor_execute", _record)
    try:
        await builder.build(max_messages=20)
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", _record)

    assert _native_preview_rows(_native_of(builder)) == rows
    assert not [s for s in statements if "tool_executions" in s and "result_json" in s]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed",
    [
        {"success": True, "data_preview": ["not", "a", "dict"], "stats": {"total_rows": 5}},
        {
            "success": True,
            "data_preview": {"columns": [{"field": "month"}], "rows": "2026-01", "row_count": 5},
            "stats": {"total_rows": 5},
        },
    ],
    ids=["preview-is-a-list", "rows-is-a-string"],
)
async def test_malformed_preview_falls_back_to_todays_history(db, malformed):
    scenario = await _seed(db, report_type="artifact_chat", result_json=malformed)
    plain = await _seed(db, report_type=None, result_json=malformed)

    _rendered, native = await _history(db, scenario, durable=True)
    _rendered, plain_native = await _history(db, plain, durable=True)

    assert native == plain_native
