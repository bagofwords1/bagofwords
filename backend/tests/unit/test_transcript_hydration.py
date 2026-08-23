"""Earlier completions' tool results must reach the next run's transcript.

The case that motivated this: a run is interrupted after N tools. Those N
results are already committed, but the next turn used to see only a digest of
them -- and after a hard kill, the completion never even leaves 'in_progress'.
Hydration must include both interrupted shapes, not just cleanly finished runs.
"""
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.ai.context.parts import Outcome, ToolResultPart
from app.ai.context.transcript_hydration import hydrate_transcript, _content_for


def _uid():
    return str(uuid.uuid4())


@pytest_asyncio.fixture
async def db():
    # Completion.sigkill is declared nullable=False with default=None, which
    # metadata.create_all takes literally; the migrated schema this runs against
    # in production allows NULL (an un-killed run has no kill time). Relax it
    # here so the fixture matches the real table rather than the declaration.
    from app.models.completion import Completion as _C
    _C.__table__.c.sigkill.nullable = True

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


async def _seed(db, *, completion_status, tool_status="success"):
    """One report, one system completion, one block pointing at one tool."""
    from app.models.report import Report
    from app.models.completion import Completion
    from app.models.completion_block import CompletionBlock
    from app.models.tool_execution import ToolExecution
    from app.models.agent_execution import AgentExecution

    report_id, comp_id, exec_id, te_id = _uid(), _uid(), _uid(), _uid()
    db.add(Report(id=report_id, title="t", slug="t-" + report_id[:8],
                  organization_id=_uid(), user_id=_uid()))
    db.add(Completion(id=comp_id, report_id=report_id, role="system", status=completion_status))
    db.add(AgentExecution(id=exec_id, completion_id=comp_id, status="in_progress"))
    db.add(ToolExecution(
        id=te_id, agent_execution_id=exec_id, tool_name="inspect_data",
        arguments_json={"q": "orders"}, status=tool_status, success=(tool_status == "success"),
        result_summary="Inspection finished",
        context_summary_json={"version": 2, "observation": {
            "summary": "Inspection finished",
            "details": "order_id | order_status | gross_amount\n1 | paid | 12.5",
        }},
    ))
    db.add(CompletionBlock(id=_uid(), completion_id=comp_id, agent_execution_id=exec_id,
                           source_type="tool", title="Inspected orders", block_index=0, tool_execution_id=te_id,
                           status="completed"))
    await db.commit()
    return report_id, comp_id


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["success", "stopped", "in_progress"])
async def test_completions_hydrate_whatever_their_ending(db, status):
    """'stopped' is the stop button; 'in_progress' is a run whose process was
    killed and which therefore never reached its finalizer."""
    report_id, comp_id = await _seed(db, completion_status=status)
    result = await hydrate_transcript(db, report_id=report_id, exclude_completion_ids=set())

    assert comp_id in result["completion_ids"], f"{status} completion was dropped"
    assert result["results"] == 1
    bodies = [p.content for t in result["turns"] for p in t.parts if isinstance(p, ToolResultPart)]
    assert any("order_status" in b for b in bodies), "observation body did not survive"


@pytest.mark.asyncio
async def test_the_current_run_is_not_replayed_to_itself(db):
    report_id, comp_id = await _seed(db, completion_status="in_progress")
    result = await hydrate_transcript(db, report_id=report_id, exclude_completion_ids={comp_id})
    assert result["completion_ids"] == set()
    assert result["turns"] == []


@pytest.mark.asyncio
async def test_a_tool_that_never_finished_is_marked_interrupted(db):
    """An elided result must never be mistakable for a successful one."""
    report_id, _ = await _seed(db, completion_status="in_progress", tool_status="in_progress")
    result = await hydrate_transcript(db, report_id=report_id, exclude_completion_ids=set())
    parts = [p for t in result["turns"] for p in t.parts if isinstance(p, ToolResultPart)]
    assert parts and parts[0].outcome is Outcome.INTERRUPTED


@pytest.mark.asyncio
async def test_the_token_budget_is_honoured(db):
    report_id, _ = await _seed(db, completion_status="success")
    result = await hydrate_transcript(db, report_id=report_id, exclude_completion_ids=set(), token_budget=1)
    # The first batch is always admitted (there is nothing to fall back to);
    # what matters is that the walk stops instead of running away.
    assert result["tokens"] <= 2000


def test_content_prefers_the_observation_over_the_bare_summary():
    body = _content_for("inspect_data", {"version": 2, "observation": {"summary": "s", "details": "d"}}, "fallback")
    assert "details" in body and "fallback" not in body


def test_content_falls_back_to_the_summary_when_nothing_was_persisted():
    assert _content_for("inspect_data", None, "just the summary") == "just the summary"
