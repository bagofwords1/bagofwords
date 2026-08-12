"""Regression guards for the bounded report read path.

Covers two defects found in review of the read-scaling work:

1. ``latest_block_for_step`` must follow completion chronology. Blocks are
   fetched ordered by ``completion_id`` (a UUID), so "last in iteration order"
   is arbitrary — the single inline Step copy could land on an old card while
   the newest card serialized ``created_step=None``.

2. Version-1 context summaries (written by pre-upgrade workers, before the
   summaries carried ui_preview/rows/step_id) must never be served as a card
   payload. The read path rebuilds them from the full JSON, and the migration
   upgrades them in place.
"""
import asyncio
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.report import Report
from app.models.widget import Widget
from app.models.query import Query
from app.models.step import Step
from app.models.completion import Completion
from app.models.agent_execution import AgentExecution
from app.models.tool_execution import ToolExecution
from app.models.completion_block import CompletionBlock
from app.services.completion_service import CompletionService
from app.ai.persisted_summary import CONTEXT_SUMMARY_VERSION


def _run(coro):
    return asyncio.run(coro)


def _rows(n):
    return [{"order_id": i, "amount": i * 1.5} for i in range(n)]


_COLUMNS = [{"field": "order_id"}, {"field": "amount"}]


async def _seed_shared_step(completion_ids):
    """Two chronological completions whose tool executions share one Step.

    ``completion_ids`` fixes the UUIDs so the test controls whether UUID order
    agrees with chronological order.
    """
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Latest Org {suffix}")
        user = User(
            name="Latest User",
            email=f"latest-{suffix}@example.com",
            hashed_password="x",
            is_active=True,
            is_verified=True,
        )
        db.add_all([org, user])
        await db.flush()

        report = Report(
            title=f"Latest Report {suffix}",
            slug=f"latest-report-{suffix}",
            status="draft",
            user_id=user.id,
            organization_id=org.id,
        )
        db.add(report)
        await db.flush()

        widget = Widget(title="W", slug=f"w-{suffix}", report_id=report.id)
        db.add(widget)
        await db.flush()
        query = Query(
            title="Q", report_id=report.id, widget_id=widget.id,
            organization_id=org.id, user_id=user.id,
        )
        db.add(query)
        await db.flush()
        step = Step(
            title="Shared step",
            slug=f"s-{suffix}",
            status="success",
            widget_id=widget.id,
            query_id=query.id,
            data={"rows": _rows(30), "columns": _COLUMNS},
            data_model={"type": "table", "columns": _COLUMNS},
            view={"type": "table"},
        )
        db.add(step)
        await db.flush()

        for turn, completion_id in enumerate(completion_ids):
            completion = Completion(
                id=completion_id,
                prompt={"content": f"turn {turn}"},
                completion={"content": f"done {turn}"},
                status="success",
                role="system",
                report_id=report.id,
                user_id=user.id,
                turn_index=turn,
            )
            db.add(completion)
            await db.flush()
            ae = AgentExecution(
                completion_id=completion.id,
                organization_id=org.id,
                user_id=user.id,
                report_id=report.id,
                status="completed",
            )
            db.add(ae)
            await db.flush()
            te = ToolExecution(
                agent_execution_id=ae.id,
                tool_name="create_and_execute_code",
                status="success",
                success=True,
                arguments_json={},
                result_json={"success": True},
                created_step_id=step.id,
            )
            db.add(te)
            await db.flush()
            db.add(CompletionBlock(
                completion_id=completion.id,
                agent_execution_id=ae.id,
                source_type="tool",
                tool_execution_id=te.id,
                block_index=0,
                title="tool",
                status="completed",
            ))

        await db.commit()
        return {"org": org, "user": user, "report_id": str(report.id)}


def _tool_blocks_by_turn(response):
    by_turn = {}
    for completion in response.completions:
        for block in completion.completion_blocks or []:
            if block.tool_execution is not None:
                by_turn[completion.turn_index] = block
    return by_turn


@pytest.mark.e2e
@pytest.mark.parametrize("uuid_order_matches_time", [True, False])
def test_shared_step_embeds_on_the_chronologically_latest_card(uuid_order_matches_time):
    """The inline Step copy must land on the newest card regardless of how
    completion UUIDs happen to sort."""
    first, second = sorted(str(uuid.uuid4()) for _ in range(2))
    if uuid_order_matches_time:
        completion_ids = [first, second]
    else:
        # The chronologically newer completion gets the SMALLER UUID, so a
        # completion_id-ordered scan sees it first and an iteration-order
        # "latest" would pick the older card.
        completion_ids = [second, first]

    async def scenario():
        seeded = await _seed_shared_step(completion_ids)
        async with async_session_maker() as db:
            service = CompletionService()
            return await service.get_completions_v2(
                db,
                seeded["report_id"],
                organization=seeded["org"],
                current_user=seeded["user"],
                limit=50,
            )

    response = _run(scenario())
    blocks = _tool_blocks_by_turn(response)
    assert set(blocks) == {0, 1}

    newest = blocks[1]
    oldest = blocks[0]
    assert newest.tool_execution.created_step is not None, (
        "the chronologically newest card must embed the shared Step inline"
    )
    assert (newest.tool_execution.created_step.data or {}).get("rows"), (
        "embedded Step preview must carry rows"
    )
    assert oldest.tool_execution.created_step is None, (
        "older cards keep only the id and hydrate on expansion"
    )


def _v1_step_summary(rows, row_count):
    """The exact step summary shape the pre-upgrade write hooks persisted."""
    return {
        "version": 1,
        "row_count": row_count,
        "columns": _COLUMNS,
        "preview_rows": rows[:5],
        "info": {"total_rows": row_count},
    }


def _v1_read_query_summary(row_count):
    """Pre-upgrade read_query summaries: no rows, no step_id, no view."""
    return {
        "version": 1,
        "success": True,
        "results": [
            {
                "query_id": "q-1",
                "title": "orders",
                "data_preview": {"columns": _COLUMNS, "row_count": row_count},
            }
        ],
        "errors": None,
    }


async def _seed_stale_summaries():
    suffix = uuid.uuid4().hex[:8]
    rows = _rows(120)
    async with async_session_maker() as db:
        org = Organization(name=f"Stale Org {suffix}")
        user = User(
            name="Stale User",
            email=f"stale-{suffix}@example.com",
            hashed_password="x",
            is_active=True,
            is_verified=True,
        )
        db.add_all([org, user])
        await db.flush()
        report = Report(
            title=f"Stale Report {suffix}",
            slug=f"stale-report-{suffix}",
            status="draft",
            user_id=user.id,
            organization_id=org.id,
        )
        db.add(report)
        await db.flush()
        widget = Widget(title="W", slug=f"w-{suffix}", report_id=report.id)
        db.add(widget)
        await db.flush()
        query = Query(
            title="Q", report_id=report.id, widget_id=widget.id,
            organization_id=org.id, user_id=user.id,
        )
        db.add(query)
        await db.flush()
        step = Step(
            title="Stale step",
            slug=f"s-{suffix}",
            status="success",
            widget_id=widget.id,
            query_id=query.id,
            data={"rows": rows, "columns": _COLUMNS, "info": {"total_rows": len(rows)}},
            data_model={"type": "table", "columns": _COLUMNS},
            view={"type": "table"},
        )
        db.add(step)
        await db.flush()

        completion = Completion(
            prompt={"content": "read"},
            completion={"content": "done"},
            status="success",
            role="system",
            report_id=report.id,
            user_id=user.id,
            turn_index=0,
        )
        db.add(completion)
        await db.flush()
        ae = AgentExecution(
            completion_id=completion.id,
            organization_id=org.id,
            user_id=user.id,
            report_id=report.id,
            status="completed",
        )
        db.add(ae)
        await db.flush()
        te = ToolExecution(
            agent_execution_id=ae.id,
            tool_name="read_query",
            status="success",
            success=True,
            arguments_json={},
            result_json={
                "success": True,
                "results": [
                    {
                        "query_id": str(query.id),
                        "step_id": str(step.id),
                        "title": "orders",
                        "data_preview": {
                            "columns": _COLUMNS,
                            "rows": rows[:50],
                            "row_count": len(rows),
                        },
                    }
                ],
            },
            created_step_id=step.id,
        )
        db.add(te)
        await db.flush()
        db.add(CompletionBlock(
            completion_id=completion.id,
            agent_execution_id=ae.id,
            source_type="tool",
            tool_execution_id=te.id,
            block_index=0,
            title="read_query",
            status="completed",
        ))
        await db.flush()

        # Overwrite the hook-written summaries with the exact version-1 shapes
        # the pre-upgrade deployment persisted.
        step.context_summary_json = _v1_step_summary(rows, len(rows))
        te.context_summary_json = _v1_read_query_summary(len(rows))
        await db.commit()
        return {"org": org, "user": user, "report_id": str(report.id)}


@pytest.mark.e2e
def test_version1_summaries_are_rebuilt_before_serving_cards():
    """A v1 summary (no ui_preview/rows/step_id) must not be served as-is."""
    async def scenario():
        seeded = await _seed_stale_summaries()
        async with async_session_maker() as db:
            service = CompletionService()
            return await service.get_completions_v2(
                db,
                seeded["report_id"],
                organization=seeded["org"],
                current_user=seeded["user"],
                limit=50,
            )

    response = _run(scenario())
    blocks = _tool_blocks_by_turn(response)
    tool = blocks[0].tool_execution
    results = (tool.result_json or {}).get("results") or []
    assert results, "read_query card must keep its per-result payload"
    preview = results[0].get("data_preview") or {}
    assert preview.get("rows"), (
        "stale v1 summaries must be rebuilt from the canonical result_json "
        "so the card keeps its bounded preview rows"
    )
    assert results[0].get("step_id"), (
        "rebuilt results must carry step_id for on-expand hydration"
    )

    step_schema = tool.created_step
    assert step_schema is not None
    step_rows = (step_schema.data or {}).get("rows") or []
    assert len(step_rows) > 5, (
        "a stale v1 step summary must not degrade the card to the 5-row "
        "prompt projection; the bounded 20-row preview comes from full data"
    )


@pytest.mark.e2e
def test_backfill_migration_upgrades_stale_v1_summaries(tmp_path):
    """The rptperf001 backfill rebuilds v1 summaries, not only NULLs."""
    import sqlalchemy as sa

    engine = sa.create_engine(f"sqlite:///{tmp_path}/backfill.db")
    metadata = sa.MetaData()
    steps = sa.Table(
        "steps", metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("data", sa.JSON),
        sa.Column("context_summary_json", sa.JSON),
    )
    tools = sa.Table(
        "tool_executions", metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("tool_name", sa.String),
        sa.Column("result_json", sa.JSON),
        sa.Column("context_summary_json", sa.JSON),
    )
    metadata.create_all(engine)

    rows = _rows(60)
    data = {"rows": rows, "columns": _COLUMNS, "info": {"total_rows": len(rows)}}
    sentinel_current = {"version": CONTEXT_SUMMARY_VERSION, "row_count": 1, "marker": "keep"}
    with engine.begin() as conn:
        conn.execute(steps.insert(), [
            {"id": "step-null", "data": data, "context_summary_json": None},
            {"id": "step-v1", "data": data, "context_summary_json": _v1_step_summary(rows, len(rows))},
            {"id": "step-current", "data": data, "context_summary_json": sentinel_current},
        ])
        conn.execute(tools.insert(), [
            {
                "id": "te-v1",
                "tool_name": "read_query",
                "result_json": {
                    "success": True,
                    "results": [{
                        "query_id": "q-1",
                        "step_id": "step-v1",
                        "data_preview": {"columns": _COLUMNS, "rows": rows[:50], "row_count": len(rows)},
                    }],
                },
                "context_summary_json": _v1_read_query_summary(len(rows)),
            },
        ])

    import importlib.util
    from pathlib import Path

    migration_path = (
        Path(__file__).resolve().parents[2]
        / "alembic" / "versions" / "rptperf001_backfill_report_payload_summaries.py"
    )
    spec = importlib.util.spec_from_file_location("rptperf001_backfill", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    _backfill_steps = migration._backfill_steps
    _backfill_tool_executions = migration._backfill_tool_executions

    with engine.begin() as conn:
        _backfill_steps(conn)
        _backfill_tool_executions(conn)

    with engine.connect() as conn:
        by_id = {
            row.id: row.context_summary_json
            for row in conn.execute(sa.select(steps.c.id, steps.c.context_summary_json))
        }
        tool_summary = conn.execute(
            sa.select(tools.c.context_summary_json).where(tools.c.id == "te-v1")
        ).scalar_one()

    for step_id in ("step-null", "step-v1"):
        summary = by_id[step_id]
        assert summary["version"] == CONTEXT_SUMMARY_VERSION
        assert isinstance(summary.get("ui_preview"), dict), step_id
        assert summary["ui_preview"]["rows"], step_id
    assert by_id["step-current"] == sentinel_current, (
        "already-current summaries must not be rewritten"
    )

    assert tool_summary["version"] == CONTEXT_SUMMARY_VERSION
    rebuilt = tool_summary["results"][0]
    assert rebuilt.get("step_id") == "step-v1"
    assert (rebuilt.get("data_preview") or {}).get("rows"), (
        "rebuilt read_query summaries must carry bounded preview rows"
    )
