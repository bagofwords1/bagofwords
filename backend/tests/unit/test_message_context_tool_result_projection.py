"""Conversation history keeps tool meaning without loading result payloads.

``read_query`` stores its outputs under ``result_json.results`` and each result
may contain the complete query dataset.  History needs the query references and
the bounded ``data_preview`` only; decoding ``results[*].data`` is both wasted
work and catastrophic for large reports.
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
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Import the same model graph Alembic registers before creating the in-memory
# schema.  The scenario is not available through a public API: completion
# blocks and historical tool executions are internal agent persistence rows.
_env_src = (Path(__file__).resolve().parents[2] / "alembic" / "env.py").read_text()
for _stmt in re.findall(
    r"^from app\.models\S* import \([^)]*\)|^from app\.models[^\n]+",
    _env_src,
    re.M,
):
    exec(_stmt)  # noqa: S102 -- test-only, mirrors alembic/env.py verbatim

from app.ai.context.builders.message_context_builder import MessageContextBuilder
from app.ai.context.parts import ToolCallPart, ToolResultPart
from app.ai.context.sections.base import xml_escape
from app.ai.persisted_summary import UI_TOOL_PREVIEW_ROWS, build_tool_context_summary
from app.dependencies import async_session_maker
from app.models.agent_execution import AgentExecution
from app.models.base import Base
from app.models.completion import Completion
from app.models.completion_block import CompletionBlock
from app.models.organization import Organization
from app.models.report import Report
from app.models.step import Step
from app.models.tool_execution import ToolExecution
from app.models.user import User

_LARGE_PAYLOAD_MARKER = "payload-that-history-must-not-hydrate-"


def test_read_query_summary_keeps_bounded_ui_preview_and_step_reference():
    rows = [{"value": index} for index in range(UI_TOOL_PREVIEW_ROWS + 7)]
    raw = {
        "success": True,
        "results": [
            {
                "query_id": "query-1",
                "step_id": "step-1",
                "title": "Saved query",
                "data": {"rows": rows * 100, "columns": [{"field": "value"}]},
                "data_preview": {
                    "rows": rows,
                    "columns": [{"field": "value"}],
                    "row_count": 2_700,
                    "truncated": True,
                },
                "data_model": {"type": "table"},
                "view": {"type": "table"},
            }
        ],
    }

    summary = build_tool_context_summary("read_query", raw)

    assert summary is not None
    result = summary["results"][0]
    assert result["step_id"] == "step-1"
    assert result["data_preview"]["row_count"] == 2_700
    assert len(result["data_preview"]["rows"]) == UI_TOOL_PREVIEW_ROWS
    assert len(raw["results"][0]["data"]["rows"]) == 2_700


def test_context_summaries_have_dedicated_lightweight_storage():
    """The fast path must not overload mutable rendering or artifact fields."""
    assert hasattr(Step, "context_summary_json")
    assert hasattr(ToolExecution, "context_summary_json")


@pytest_asyncio.fixture
async def db_and_hydrations():
    """Track JSON values decoded from SQLite into Python during the test."""
    hydrated_large_payloads: list[int] = []

    def _tracking_json_loads(value: str):
        if _LARGE_PAYLOAD_MARKER in value:
            hydrated_large_payloads.append(len(value))
        return json.loads(value)

    # The migration-backed schema permits NULL here; align metadata.create_all
    # with it for this isolated unit database.
    Completion.__table__.c.sigkill.nullable = True
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        json_deserializer=_tracking_json_loads,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as db:
        yield db, hydrated_large_payloads
    await engine.dispose()


@pytest_asyncio.fixture
async def migrated_db():
    """Use the Alembic-backed database selected by the test ``--db`` option."""
    async with async_session_maker() as session:
        yield session
        await session.rollback()


async def _seed_nested_read_query_result(
    db: AsyncSession,
    *,
    legacy_projection: bool = False,
):
    user = User(
        name="History reader",
        email=f"history-{uuid.uuid4()}@example.test",
        hashed_password="not-used",
    )
    db.add(user)
    await db.flush()

    organization = Organization(name=f"History org {uuid.uuid4()}")
    db.add(organization)
    await db.flush()

    report = Report(
        title="Query history",
        slug=f"query-history-{uuid.uuid4()}",
        user_id=str(user.id),
        organization_id=str(organization.id),
    )
    db.add(report)
    await db.flush()

    user_completion = Completion(
        prompt={"content": "Use the regional revenue query."},
        completion={},
        status="success",
        model="test",
        role="user",
        message_type="ai_completion",
        report_id=str(report.id),
        user_id=str(user.id),
    )
    system_completion = Completion(
        prompt={"content": ""},
        completion={"content": "I inspected the saved query."},
        status="success",
        model="test",
        role="system",
        message_type="ai_completion",
        report_id=str(report.id),
    )
    db.add_all([user_completion, system_completion])
    await db.flush()

    execution = AgentExecution(
        completion_id=str(system_completion.id),
        organization_id=str(organization.id),
        user_id=str(user.id),
        report_id=str(report.id),
        status="success",
    )
    db.add(execution)
    await db.flush()

    query_id = str(uuid.uuid4())
    visualization_id = str(uuid.uuid4())
    title = "Regional net revenue"
    row_count = 101_039
    columns = [{"field": "region"}, {"field": "net_revenue"}]
    # A single wide value is enough to tell whether SQLAlchemy decoded the
    # complete stored result. Repeating 100k rows would only make the test slow.
    large_value = _LARGE_PAYLOAD_MARKER * 40_000
    tool_execution = ToolExecution(
        agent_execution_id=str(execution.id),
        tool_name="read_query",
        tool_action="read",
        arguments_json={"query_ids": [query_id]},
        status="success",
        success=True,
        result_summary="Read one saved query",
        result_json={
            "success": True,
            "results": [
                {
                    "query_id": query_id,
                    "visualization_id": visualization_id,
                    "title": title,
                    "data": {
                        "columns": columns,
                        "rows": [{"region": large_value, "net_revenue": 42}],
                    },
                    "data_preview": {
                        "columns": columns,
                        "rows": [{"region": "North", "net_revenue": 42}],
                        "row_count": row_count,
                        "truncated": True,
                    },
                }
            ],
        },
    )
    db.add(tool_execution)
    await db.flush()
    db.add(
        CompletionBlock(
            completion_id=str(system_completion.id),
            agent_execution_id=str(execution.id),
            source_type="tool",
            tool_execution_id=str(tool_execution.id),
            block_index=0,
            title="Read query",
            status="completed",
        )
    )
    await db.commit()
    if legacy_projection:
        # Simulate a pre-migration row. A bulk update intentionally bypasses
        # the model event that snapshots summaries for new writes.
        await db.execute(
            update(ToolExecution).where(ToolExecution.id == str(tool_execution.id)).values(context_summary_json=None)
        )
        await db.commit()

    return SimpleNamespace(
        organization_id=str(organization.id),
        report_id=str(report.id),
        user_id=str(user.id),
        tool_execution_id=str(tool_execution.id),
        query_id=query_id,
        visualization_id=visualization_id,
        title=title,
        row_count=row_count,
        column_count=len(columns),
    )


async def _seed_legacy_create_data_result(
    db: AsyncSession,
    *,
    legacy_projection: bool = True,
):
    """Add a pre-data_preview create_data execution to a real history turn."""
    scenario = await _seed_nested_read_query_result(db)
    execution = (
        await db.execute(
            select(AgentExecution).where(
                AgentExecution.report_id == scenario.report_id,
            )
        )
    ).scalar_one()

    first_row = {"region": "North", "net_revenue": 42}
    later_row = {
        "region": _LARGE_PAYLOAD_MARKER * 40_000,
        "net_revenue": 99,
    }
    tool_execution = ToolExecution(
        agent_execution_id=str(execution.id),
        tool_name="create_data",
        tool_action="create",
        arguments_json={"description": "Regional revenue"},
        status="success",
        success=True,
        result_summary="Created regional revenue data",
        # Historical shape: full data existed before data_preview was added.
        result_json={
            "success": True,
            "data": {
                "columns": [
                    {"field": "region"},
                    {"field": "net_revenue"},
                ],
                "rows": [first_row, later_row],
            },
            "data_model": {"type": "table"},
        },
    )
    db.add(tool_execution)
    await db.flush()
    db.add(
        CompletionBlock(
            completion_id=str(execution.completion_id),
            agent_execution_id=str(execution.id),
            source_type="tool",
            tool_execution_id=str(tool_execution.id),
            block_index=1,
            title="Create data",
            status="completed",
        )
    )
    await db.commit()
    if legacy_projection:
        await db.execute(
            update(ToolExecution).where(ToolExecution.id == str(tool_execution.id)).values(context_summary_json=None)
        )
        await db.commit()

    scenario.legacy_first_row = first_row
    scenario.legacy_row_count = 2
    scenario.legacy_column_count = 2
    return scenario


async def _build_history(db: AsyncSession, scenario):
    # A fresh identity map makes SQL loading observable instead of allowing the
    # seeded ToolExecution instance to satisfy history construction from memory.
    db.expunge_all()
    organization = (
        (await db.execute(select(Organization).where(Organization.id == scenario.organization_id)))
        .unique()
        .scalar_one()
    )
    report_ref = SimpleNamespace(id=scenario.report_id)
    return await MessageContextBuilder(db, organization, report_ref).build(max_messages=20)


@pytest.mark.asyncio
async def test_nested_read_query_history_keeps_referenceable_digest(db_and_hydrations):
    db, _hydrated_large_payloads = db_and_hydrations
    scenario = await _seed_nested_read_query_result(db)

    rendered = (await _build_history(db, scenario)).render()

    # Assert the semantic contract, not exact punctuation: a future planner can
    # identify and reason about the stored query without seeing its full rows.
    assert "Tool: read_query" in rendered
    assert scenario.title in rendered
    assert scenario.query_id in rendered
    assert scenario.visualization_id in rendered
    assert str(scenario.row_count) in rendered.replace(",", "")
    assert re.search(rf"\b{scenario.column_count}\s+(?:cols|columns)\b", rendered)
    assert _LARGE_PAYLOAD_MARKER not in rendered


@pytest.mark.asyncio
async def test_native_history_moves_tool_details_without_prompt_duplication(
    db_and_hydrations,
):
    """Agent opt-in gets typed turns while XML keeps only dialogue text."""
    db, hydrated_large_payloads = db_and_hydrations
    scenario = await _seed_nested_read_query_result(db)
    hydrated_large_payloads.clear()
    db.expunge_all()
    organization = (
        (
            await db.execute(
                select(Organization).where(
                    Organization.id == scenario.organization_id
                )
            )
        )
        .unique()
        .scalar_one()
    )
    builder = MessageContextBuilder(
        db, organization, SimpleNamespace(id=scenario.report_id)
    )
    builder.use_durable_transcript = True

    section = await builder.build(max_messages=20)
    transcript = builder.get_durable_transcript()
    rendered = section.render()

    assert "Tool: read_query" not in rendered
    calls = [
        part
        for turn in transcript.turns
        for part in turn.parts
        if isinstance(part, ToolCallPart)
    ]
    results = [
        part
        for turn in transcript.turns
        for part in turn.parts
        if isinstance(part, ToolResultPart)
    ]
    assert len(calls) == len(results) == 1
    assert calls[0].id == results[0].call_id
    assert scenario.query_id in results[0].content
    assert _LARGE_PAYLOAD_MARKER not in results[0].content
    assert hydrated_large_payloads == []


@pytest.mark.asyncio
async def test_native_history_reuses_the_existing_query_budget(db_and_hydrations):
    """Durable replay changes one SELECT's scope; it does not add a SELECT."""
    db, _hydrated_large_payloads = db_and_hydrations
    scenario = await _seed_nested_read_query_result(db)
    db.expunge_all()
    organization = (
        (
            await db.execute(
                select(Organization).where(
                    Organization.id == scenario.organization_id
                )
            )
        )
        .unique()
        .scalar_one()
    )

    async def _query_count(*, native: bool) -> int:
        builder = MessageContextBuilder(
            db, organization, SimpleNamespace(id=scenario.report_id)
        )
        builder.use_durable_transcript = native
        count = 0

        def _record(*_args):
            nonlocal count
            count += 1

        event.listen(db.bind.sync_engine, "before_cursor_execute", _record)
        try:
            await builder.build(max_messages=20)
        finally:
            event.remove(db.bind.sync_engine, "before_cursor_execute", _record)
        return count

    legacy_count = await _query_count(native=False)
    native_count = await _query_count(native=True)

    assert native_count <= legacy_count


@pytest.mark.asyncio
async def test_nested_read_query_history_does_not_hydrate_complete_data(db_and_hydrations):
    db, hydrated_large_payloads = db_and_hydrations
    scenario = await _seed_nested_read_query_result(db)

    # Ignore any fixture/setup reads; this assertion concerns history building.
    hydrated_large_payloads.clear()
    statements: list[str] = []

    def _record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    event.listen(db.bind.sync_engine, "before_cursor_execute", _record_sql)
    try:
        await _build_history(db, scenario)
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", _record_sql)

    assert hydrated_large_payloads == [], (
        "message history decoded read_query results[*].data instead of loading only the bounded digest fields"
    )
    assert not any("tool_executions" in statement and "result_json" in statement for statement in statements), (
        "summary-backed history still asked the database to parse result_json"
    )


@pytest.mark.asyncio
async def test_migration_backed_summary_path_never_selects_result_json(migrated_db):
    scenario = await _seed_nested_read_query_result(migrated_db)
    statements: list[str] = []

    def _record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    event.listen(migrated_db.bind.sync_engine, "before_cursor_execute", _record_sql)
    try:
        rendered = (await _build_history(migrated_db, scenario)).render()
    finally:
        event.remove(migrated_db.bind.sync_engine, "before_cursor_execute", _record_sql)

    assert scenario.query_id in rendered
    assert scenario.visualization_id in rendered
    assert not any("tool_executions" in statement and "result_json" in statement for statement in statements)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["read_query", "create_data"])
async def test_terminal_legacy_tool_projection_is_persisted_after_first_read(
    migrated_db,
    tool_name,
):
    """Any active legacy report pays its historical JSON cost at most once."""
    if tool_name == "read_query":
        scenario = await _seed_nested_read_query_result(
            migrated_db,
            legacy_projection=True,
        )
        target_execution_id = scenario.tool_execution_id
    else:
        scenario = await _seed_legacy_create_data_result(
            migrated_db,
            legacy_projection=True,
        )
        target_execution_id = (
            await migrated_db.execute(
                select(ToolExecution.id)
                .join(AgentExecution)
                .where(AgentExecution.report_id == scenario.report_id)
                .where(ToolExecution.tool_name == "create_data")
            )
        ).scalar_one()

    first_rendered = (await _build_history(migrated_db, scenario)).render()

    async with async_session_maker() as verification_db:
        persisted = (
            await verification_db.execute(
                select(ToolExecution.context_summary_json).where(ToolExecution.id == str(target_execution_id))
            )
        ).scalar_one()
    assert isinstance(persisted, dict)

    statements: list[str] = []
    async with async_session_maker() as second_db:
        organization = (
            (await second_db.execute(select(Organization).where(Organization.id == scenario.organization_id)))
            .unique()
            .scalar_one()
        )

        def _record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement.lower())

        event.listen(second_db.bind.sync_engine, "before_cursor_execute", _record_sql)
        try:
            second_rendered = (
                await MessageContextBuilder(
                    second_db,
                    organization,
                    SimpleNamespace(id=scenario.report_id),
                ).build(max_messages=20)
            ).render()
        finally:
            event.remove(second_db.bind.sync_engine, "before_cursor_execute", _record_sql)

    assert second_rendered == first_rendered
    assert not any("tool_executions" in statement and "result_json" in statement for statement in statements)


@pytest.mark.asyncio
async def test_nonterminal_legacy_tool_projection_is_not_persisted(migrated_db):
    """A changing result must never be frozen into the read-through cache."""
    scenario = await _seed_nested_read_query_result(
        migrated_db,
        legacy_projection=True,
    )
    await migrated_db.execute(
        update(ToolExecution)
        .where(ToolExecution.id == scenario.tool_execution_id)
        .values(status="in_progress", success=False)
    )
    await migrated_db.commit()

    await _build_history(migrated_db, scenario)

    async with async_session_maker() as verification_db:
        persisted = (
            await verification_db.execute(
                select(ToolExecution.context_summary_json).where(ToolExecution.id == scenario.tool_execution_id)
            )
        ).scalar_one()
    assert persisted is None


@pytest.mark.asyncio
async def test_mixed_read_query_history_projects_only_legacy_result(
    db_and_hydrations,
):
    db, _hydrated_large_payloads = db_and_hydrations
    scenario = await _seed_nested_read_query_result(db)
    execution = (
        await db.execute(
            select(AgentExecution).where(
                AgentExecution.report_id == scenario.report_id,
            )
        )
    ).scalar_one()

    legacy_query_id = str(uuid.uuid4())
    legacy_visualization_id = str(uuid.uuid4())
    legacy_execution = ToolExecution(
        agent_execution_id=str(execution.id),
        tool_name="read_query",
        tool_action="read",
        arguments_json={"query_ids": [legacy_query_id]},
        status="success",
        success=True,
        result_summary="Read one legacy query",
        result_json={
            "success": True,
            "results": [
                {
                    "query_id": legacy_query_id,
                    "visualization_id": legacy_visualization_id,
                    "title": "Legacy customer retention",
                    "data": {
                        "columns": [{"field": "cohort"}],
                        "rows": [{"cohort": _LARGE_PAYLOAD_MARKER * 10_000}],
                    },
                    "data_preview": {
                        "columns": [{"field": "cohort"}],
                        "rows": [{"cohort": "2026-Q1"}],
                        "row_count": 12,
                    },
                }
            ],
        },
    )
    db.add(legacy_execution)
    await db.flush()
    db.add(
        CompletionBlock(
            completion_id=str(execution.completion_id),
            agent_execution_id=str(execution.id),
            source_type="tool",
            tool_execution_id=str(legacy_execution.id),
            block_index=1,
            title="Read legacy query",
            status="completed",
        )
    )
    await db.commit()
    await db.execute(
        update(ToolExecution).where(ToolExecution.id == str(legacy_execution.id)).values(context_summary_json=None)
    )
    await db.commit()

    projection_parameters: list[tuple[str, ...]] = []

    def _record_projection(
        _conn,
        _cursor,
        statement,
        parameters,
        _context,
        _executemany,
    ):
        normalized = statement.lower()
        if "json_each" in normalized and "tool_executions" in normalized:
            projection_parameters.append(tuple(str(value) for value in parameters))

    event.listen(db.bind.sync_engine, "before_cursor_execute", _record_projection)
    try:
        rendered = (await _build_history(db, scenario)).render()
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", _record_projection)

    assert scenario.query_id in rendered
    assert legacy_query_id in rendered
    assert projection_parameters == [(str(legacy_execution.id),)]
    assert scenario.tool_execution_id not in projection_parameters[0]


@pytest.mark.asyncio
async def test_create_data_summary_keeps_digest_without_result_json(
    db_and_hydrations,
):
    db, hydrated_large_payloads = db_and_hydrations
    scenario = await _seed_legacy_create_data_result(
        db,
        legacy_projection=False,
    )
    hydrated_large_payloads.clear()
    statements: list[str] = []

    def _record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    event.listen(db.bind.sync_engine, "before_cursor_execute", _record_sql)
    try:
        rendered = (await _build_history(db, scenario)).render()
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", _record_sql)

    assert f"{scenario.legacy_row_count} rows × {scenario.legacy_column_count} cols" in rendered
    assert f"top row: {xml_escape(json.dumps(scenario.legacy_first_row))}" in rendered
    assert _LARGE_PAYLOAD_MARKER not in rendered
    assert hydrated_large_payloads == []
    assert not any("tool_executions" in statement and "result_json" in statement for statement in statements)


@pytest.mark.asyncio
async def test_legacy_create_data_history_keeps_first_row_without_decoding_later_rows(
    db_and_hydrations,
):
    db, hydrated_large_payloads = db_and_hydrations
    scenario = await _seed_legacy_create_data_result(db)

    # Ignore setup reads; only history construction is under test.
    hydrated_large_payloads.clear()
    rendered = (await _build_history(db, scenario)).render()

    assert "Tool: create_data" in rendered
    assert (f"{scenario.legacy_row_count} rows × {scenario.legacy_column_count} cols") in rendered
    assert f"top row: {xml_escape(json.dumps(scenario.legacy_first_row))}" in rendered
    assert _LARGE_PAYLOAD_MARKER not in rendered
    assert hydrated_large_payloads == [], (
        "message history decoded the complete legacy create_data rows instead "
        "of projecting only the historical first-row sample"
    )


@pytest.mark.asyncio
async def test_terminal_read_query_summary_is_reused_across_unchanged_builds(
    db_and_hydrations,
):
    db, _hydrated_large_payloads = db_and_hydrations
    scenario = await _seed_nested_read_query_result(db)

    db.expunge_all()
    organization = (
        (await db.execute(select(Organization).where(Organization.id == scenario.organization_id)))
        .unique()
        .scalar_one()
    )
    builder = MessageContextBuilder(
        db,
        organization,
        SimpleNamespace(id=scenario.report_id),
    )

    summary_statements: list[str] = []

    def _record_projection(
        _conn,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        normalized = statement.lower()
        if "from tool_executions" in normalized and "context_summary_json" in normalized:
            summary_statements.append(normalized)

    event.listen(db.bind.sync_engine, "before_cursor_execute", _record_projection)
    try:
        first = await builder.build(max_messages=20)
        assert scenario.query_id in first.render()
        assert len(summary_statements) == 1

        summary_statements.clear()
        second = await builder.build(max_messages=20)
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", _record_projection)

    assert scenario.query_id in second.render()
    assert summary_statements == [], (
        "an unchanged terminal read_query summary was loaded again instead of reusing its bounded history digest"
    )


@pytest.mark.asyncio
async def test_nonterminal_read_query_projection_refreshes_after_result_changes(
    db_and_hydrations,
):
    db, _hydrated_large_payloads = db_and_hydrations
    scenario = await _seed_nested_read_query_result(db, legacy_projection=True)
    await db.execute(
        update(ToolExecution)
        .where(ToolExecution.id == scenario.tool_execution_id)
        .values(status="in_progress", success=False)
    )
    await db.commit()

    db.expunge_all()
    organization = (
        (await db.execute(select(Organization).where(Organization.id == scenario.organization_id)))
        .unique()
        .scalar_one()
    )
    builder = MessageContextBuilder(
        db,
        organization,
        SimpleNamespace(id=scenario.report_id),
    )

    projection_statements: list[str] = []

    def _record_projection(
        _conn,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        normalized = statement.lower()
        if "json_each" in normalized and "tool_executions" in normalized:
            projection_statements.append(normalized)

    event.listen(db.bind.sync_engine, "before_cursor_execute", _record_projection)
    try:
        first = await builder.build(max_messages=20)
        assert "Tool: read_query" in first.render()
        assert len(projection_statements) == 1

        updated_query_id = str(uuid.uuid4())
        updated_visualization_id = str(uuid.uuid4())
        updated_title = "Regional revenue after refresh"
        updated_result = {
            "success": True,
            "results": [
                {
                    "query_id": updated_query_id,
                    "visualization_id": updated_visualization_id,
                    "title": updated_title,
                    "data": {
                        "columns": [{"field": "region"}],
                        "rows": [
                            {"region": _LARGE_PAYLOAD_MARKER * 20_000},
                        ],
                    },
                    "data_preview": {
                        "columns": [{"field": "region"}],
                        "rows": [{"region": "South"}],
                        "row_count": 7,
                    },
                }
            ],
        }
        await db.execute(
            update(ToolExecution)
            .where(ToolExecution.id == scenario.tool_execution_id)
            .values(
                status="success",
                success=True,
                result_summary="Read refreshed query",
                result_json=updated_result,
            )
        )
        await db.commit()

        projection_statements.clear()
        second = await builder.build(max_messages=20)
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", _record_projection)

    rendered = second.render()
    assert len(projection_statements) == 1
    assert updated_title in rendered
    assert updated_query_id in rendered
    assert updated_visualization_id in rendered
    assert "7 rows" in rendered


@pytest.mark.asyncio
async def test_terminal_projection_cache_only_loads_new_visible_execution(
    db_and_hydrations,
):
    db, _hydrated_large_payloads = db_and_hydrations
    scenario = await _seed_nested_read_query_result(db)

    db.expunge_all()
    organization = (
        (await db.execute(select(Organization).where(Organization.id == scenario.organization_id)))
        .unique()
        .scalar_one()
    )
    builder = MessageContextBuilder(
        db,
        organization,
        SimpleNamespace(id=scenario.report_id),
    )

    summary_calls: list[tuple[str, tuple[str, ...]]] = []

    def _record_projection(
        _conn,
        _cursor,
        statement,
        parameters,
        _context,
        _executemany,
    ):
        normalized = statement.lower()
        if "from tool_executions" in normalized and "context_summary_json" in normalized:
            summary_calls.append((normalized, tuple(str(value) for value in parameters)))

    event.listen(db.bind.sync_engine, "before_cursor_execute", _record_projection)
    try:
        first = await builder.build(max_messages=20)
        assert scenario.title in first.render()
        assert len(summary_calls) == 1
        assert summary_calls[0][1] == (scenario.tool_execution_id,)

        second_completion = Completion(
            prompt={"content": ""},
            completion={"content": "I inspected another saved query."},
            status="success",
            model="test",
            role="system",
            message_type="ai_completion",
            report_id=scenario.report_id,
        )
        db.add(second_completion)
        await db.flush()
        second_execution = AgentExecution(
            completion_id=str(second_completion.id),
            organization_id=scenario.organization_id,
            user_id=scenario.user_id,
            report_id=scenario.report_id,
            status="success",
        )
        db.add(second_execution)
        await db.flush()

        second_query_id = str(uuid.uuid4())
        second_visualization_id = str(uuid.uuid4())
        second_title = "Customer retention detail"
        second_tool_execution = ToolExecution(
            agent_execution_id=str(second_execution.id),
            tool_name="read_query",
            tool_action="read",
            arguments_json={"query_ids": [second_query_id]},
            status="success",
            success=True,
            result_summary="Read another saved query",
            result_json={
                "success": True,
                "results": [
                    {
                        "query_id": second_query_id,
                        "visualization_id": second_visualization_id,
                        "title": second_title,
                        "data": {
                            "columns": [{"field": "cohort"}],
                            "rows": [
                                {"cohort": _LARGE_PAYLOAD_MARKER * 10_000},
                            ],
                        },
                        "data_preview": {
                            "columns": [{"field": "cohort"}],
                            "rows": [{"cohort": "2026-Q1"}],
                            "row_count": 12,
                        },
                    }
                ],
            },
        )
        db.add(second_tool_execution)
        await db.flush()
        db.add(
            CompletionBlock(
                completion_id=str(second_completion.id),
                agent_execution_id=str(second_execution.id),
                source_type="tool",
                tool_execution_id=str(second_tool_execution.id),
                block_index=0,
                title="Read another query",
                status="completed",
            )
        )
        await db.commit()

        summary_calls.clear()
        second = await builder.build(max_messages=20)
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", _record_projection)

    rendered = second.render()
    assert scenario.title in rendered
    assert scenario.query_id in rendered
    assert second_title in rendered
    assert second_query_id in rendered
    assert second_visualization_id in rendered
    assert len(summary_calls) == 1
    assert summary_calls[0][1] == (str(second_tool_execution.id),)
    assert scenario.tool_execution_id not in summary_calls[0][1]
