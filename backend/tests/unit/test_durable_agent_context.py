"""Durable agent-context invariants across completion and process boundaries."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.ai.context.durable_transcript import build_durable_transcript
from app.ai.context.parts import Outcome, ToolCallPart, ToolResultPart
from app.ai.persisted_summary import (
    GENERIC_TOOL_CONTEXT_BUDGET_BYTES,
    build_tool_context_summary,
    tool_context_for_replay,
)
from app.dependencies import async_session_maker
from app.models.agent_execution import AgentExecution
from app.models.completion import Completion
from app.models.completion_block import CompletionBlock
from app.models.organization import Organization
from app.models.report import Report
from app.models.tool_execution import ToolExecution
from app.models.user import User
from app.project_manager import ProjectManager
from app.schemas.ai.planner import Action, PlannerDecision


async def _seed_execution(db) -> AgentExecution:
    user = User(
        name="Durability tester",
        email=f"durability-{uuid.uuid4()}@example.test",
        hashed_password="not-used",
    )
    organization = Organization(name=f"Durability org {uuid.uuid4()}")
    db.add_all([user, organization])
    await db.flush()

    report = Report(
        title="Durable context",
        slug=f"durable-context-{uuid.uuid4()}",
        user_id=str(user.id),
        organization_id=str(organization.id),
    )
    db.add(report)
    await db.flush()

    completion = Completion(
        report_id=str(report.id),
        user_id=str(user.id),
        role="system",
        status="in_progress",
        prompt={"content": "Inspect the source and remember the exact result."},
        completion={},
    )
    db.add(completion)
    await db.flush()

    execution = AgentExecution(
        completion_id=str(completion.id),
        organization_id=str(organization.id),
        user_id=str(user.id),
        report_id=str(report.id),
        status="in_progress",
    )
    db.add(execution)
    await db.commit()
    return execution


@pytest.mark.asyncio
async def test_tool_intent_is_durable_before_dispatch():
    """A process death after dispatch must not erase evidence of the call.

    The returned execution ID is what the tool runner and UI use. A separate
    database session must be able to recover that exact row before any side
    effect begins; an in-memory stub cannot provide the guarantee.
    """

    manager = ProjectManager()
    async with async_session_maker() as db:
        execution = await _seed_execution(db)
        tool_execution = await manager.start_tool_execution(
            db,
            agent_execution=execution,
            plan_decision_id=None,
            tool_name="inspect_data",
            tool_action="research",
            arguments_json={"step_id": "step-before-dispatch"},
        )

    async with async_session_maker() as verification_db:
        persisted = (
            await verification_db.execute(
                select(ToolExecution).where(ToolExecution.id == str(tool_execution.id))
            )
        ).scalar_one_or_none()

    assert persisted is not None, (
        "tool intent must commit before dispatch; otherwise SIGKILL can leave an "
        "invisible side effect that a follow-up may repeat"
    )
    assert persisted.status == "in_progress"
    assert persisted.arguments_json == {"step_id": "step-before-dispatch"}


@pytest.mark.asyncio
async def test_retrospective_provider_event_finishes_as_one_canonical_row():
    """Already-completed provider events do not need a pre-result write."""
    manager = ProjectManager()
    async with async_session_maker() as db:
        execution = await _seed_execution(db)
        tool_execution = await manager.start_tool_execution(
            db,
            agent_execution=execution,
            plan_decision_id=None,
            tool_name="web_search",
            tool_action="search",
            arguments_json={"query": "durable context"},
            persist_before_dispatch=False,
        )
        finished = await manager.finish_tool_execution(
            db,
            tool_execution=tool_execution,
            status="success",
            success=True,
            result_summary="one result",
            result_json={"summary": "one result", "source_id": "source-17"},
        )
        rows = (
            await db.execute(
                select(ToolExecution).where(ToolExecution.id == str(finished.id))
            )
        ).scalars().all()

    assert len(rows) == 1
    assert rows[0].status == "success"
    assert rows[0].context_summary_json["source_id"] == "source-17"


@pytest.mark.asyncio
async def test_completed_ordinary_tool_replays_the_model_visible_observation():
    """Later completions receive what the model saw, not a different output shape."""
    manager = ProjectManager()
    async with async_session_maker() as db:
        execution = await _seed_execution(db)
        tool_execution = await manager.start_tool_execution(
            db,
            agent_execution=execution,
            plan_decision_id=None,
            tool_name="inspect_data",
            tool_action="research",
            arguments_json={"question": "Which order statuses exist?"},
        )
        observation = {
            "summary": "Inspection finished",
            "details": "status values: paid, refunded, cancelled",
            "success": True,
        }
        finished = await manager.finish_tool_execution_from_models(
            db,
            tool_execution=tool_execution,
            result_model={
                "success": True,
                "execution_log": "canonical output uses a different field name",
            },
            summary=observation["summary"],
            success=True,
            observation=observation,
        )

        transcript, _ = build_durable_transcript(
            completions=[SimpleNamespace(id="completion", role="system")],
            blocks_by_completion={
                "completion": [
                    SimpleNamespace(
                        tool_execution_id=str(finished.id),
                        plan_decision_id=None,
                        agent_execution_id=str(execution.id),
                        block_index=1,
                    )
                ]
            },
            tool_exec_by_id={str(finished.id): finished},
        )

    replayed = next(
        part
        for turn in transcript.turns
        for part in turn.parts
        if isinstance(part, ToolResultPart)
    )
    assert "paid, refunded, cancelled" in replayed.content
    assert "different field name" not in replayed.content
    assert finished.result_json["execution_log"] == "canonical output uses a different field name"


@pytest.mark.asyncio
async def test_parallel_intents_share_the_existing_decision_commit_and_finish_in_place():
    """Provider order/identity are durable and completion does not duplicate rows."""
    manager = ProjectManager()
    async with async_session_maker() as db:
        execution = await _seed_execution(db)
        decision_model = PlannerDecision(
            analysis_complete=False,
            plan_type="research",
            actions=[
                Action(
                    type="tool_call",
                    name="inspect_data",
                    arguments={"step_id": "alpha"},
                    id="provider-call-a",
                    signature="opaque-a",
                    provider="google",
                ),
                Action(
                    type="tool_call",
                    name="create_instruction",
                    arguments={"title": "Remember alpha"},
                    id="provider-call-b",
                    provider="google",
                ),
            ],
        )
        decision = await manager.save_plan_decision_from_model(
            db,
            agent_execution=execution,
            seq=1,
            loop_index=0,
            planner_decision_model=decision_model,
            persist_tool_intents=True,
        )
        staged = list(decision.durable_tool_executions)
        assert [row.provider_call_id for row in staged] == [
            "provider-call-a",
            "provider-call-b",
        ]

        persisted_before_dispatch = (
            await db.execute(
                select(ToolExecution)
                .where(ToolExecution.plan_decision_id == str(decision.id))
                .order_by(ToolExecution.action_index)
            )
        ).scalars().all()
        assert [row.action_index for row in persisted_before_dispatch] == [0, 1]
        assert all(row.status == "in_progress" for row in persisted_before_dispatch)

        block = CompletionBlock(
            completion_id=str(execution.completion_id),
            agent_execution_id=str(execution.id),
            source_type="decision",
            plan_decision_id=str(decision.id),
            tool_execution_id=None,
            block_index=100,
            loop_index=0,
            title="Inspect",
        )
        db.add(block)
        await db.commit()

        await manager.finish_tool_execution(
            db,
            tool_execution=staged[0],
            status="success",
            success=True,
            result_json={
                "summary": "alpha was inspected",
                "sentinel": "durable-result-sentinel",
            },
            result_summary="alpha was inspected",
        )
        await manager.commit_tool_and_attach_block(
            db,
            completion=await db.get(Completion, str(execution.completion_id)),
            agent_execution=execution,
            tool_execution=staged[0],
            block_id=str(block.id),
        )

        count = (
            await db.execute(
                select(func.count(ToolExecution.id)).where(
                    ToolExecution.plan_decision_id == str(decision.id)
                )
            )
        ).scalar_one()
        finished = await db.get(ToolExecution, str(staged[0].id))

    assert count == 2, "finishing must update the pre-dispatch row, not insert a copy"
    assert finished.status == "success"
    assert finished.context_summary_json["sentinel"] == "durable-result-sentinel"


def test_generic_context_summary_is_bounded_and_keeps_referenceable_results():
    result = {
        "summary": "inspection complete",
        "instruction_id": "instruction-123",
        "observation": {
            "important_value": 41,
            "rows": [{"value": index, "padding": "x" * 5000} for index in range(1000)],
        },
        "images": [{"base64": "A" * 1_000_000}],
    }
    summary = build_tool_context_summary("inspect_data", result)
    encoded = json.dumps(summary).encode("utf-8")

    assert len(encoded) <= GENERIC_TOOL_CONTEXT_BUDGET_BYTES
    assert summary["summary"] == "inspection complete"
    assert summary["instruction_id"] == "instruction-123"
    assert summary["observation"]["important_value"] == 41
    assert summary["_context_truncated"] is True
    assert "A" * 100 not in encoded.decode("utf-8")


def test_observation_projection_is_bounded_media_free_and_unambiguous():
    observation = {
        "summary": "model-visible result",
        "details": "d" * 50_000,
        "version": "domain-version",
        "images": [{"base64": "A" * 1_000_000}],
    }
    summary = build_tool_context_summary(
        "inspect_data",
        {"execution_log": "canonical output"},
        observation=observation,
    )
    replayed = tool_context_for_replay(summary)

    assert len(json.dumps(summary).encode("utf-8")) <= GENERIC_TOOL_CONTEXT_BUDGET_BYTES
    assert replayed["summary"] == "model-visible result"
    assert replayed["version"] == "domain-version"
    assert replayed["images"] == "[media elided]"
    assert "canonical output" not in json.dumps(replayed)
    assert "A" * 100 not in json.dumps(summary)


def test_observation_only_tool_projects_without_a_canonical_result():
    summary = build_tool_context_summary(
        "search_mcps",
        None,
        observation={"summary": "three tools found", "tool_count": 3},
    )

    assert tool_context_for_replay(summary)["tool_count"] == 3


def test_legacy_result_with_observation_key_is_not_mistaken_for_snapshot_envelope():
    legacy = {
        "version": 2,
        "summary": "legacy wrapper",
        "observation": {"value": 41},
    }

    assert tool_context_for_replay(legacy) is legacy


def test_row_heavy_tool_keeps_its_specialized_projection_when_observation_exists():
    result = {
        "success": True,
        "data_preview": {
            "columns": [{"field": "status"}],
            "rows": [{"status": "paid"}],
            "row_count": 12,
        },
        "stats": {"total_rows": 12},
    }
    summary = build_tool_context_summary(
        "create_data",
        result,
        observation={"summary": "do not replace the row projection"},
    )

    assert summary["data_preview"]["row_count"] == 12
    assert "observation" not in summary


def test_rehydration_preserves_parallel_order_provider_identity_and_unknown_outcome():
    completion = SimpleNamespace(id="completion-1", role="system")
    blocks = [
        SimpleNamespace(
            tool_execution_id="te-a",
            plan_decision_id="decision-1",
            agent_execution_id="execution-1",
            block_index=100,
        ),
        SimpleNamespace(
            tool_execution_id="te-b",
            plan_decision_id="decision-1",
            agent_execution_id="execution-1",
            block_index=101,
        ),
    ]
    tools = {
        "te-a": SimpleNamespace(
            id="te-a",
            action_index=0,
            provider_call_id="call-a",
            provider_name="google",
            provider_signature="sig-a",
            tool_name="inspect_data",
            arguments_json={"step_id": "one"},
            status="success",
            context_summary_json={"summary": "first", "value": 41},
            result_json=None,
            result_summary="first",
            error_message=None,
        ),
        "te-b": SimpleNamespace(
            id="te-b",
            action_index=1,
            provider_call_id="call-b",
            provider_name="google",
            provider_signature=None,
            tool_name="create_instruction",
            arguments_json={"title": "second"},
            status="in_progress",
            context_summary_json=None,
            result_json=None,
            result_summary=None,
            error_message=None,
        ),
    }

    transcript, represented = build_durable_transcript(
        completions=[completion],
        blocks_by_completion={"completion-1": blocks},
        tool_exec_by_id=tools,
    )

    assert represented == {"te-a", "te-b"}
    assert len(transcript.turns) == 2
    calls = [part for part in transcript.turns[0].parts if isinstance(part, ToolCallPart)]
    results = [part for part in transcript.turns[1].parts if isinstance(part, ToolResultPart)]
    assert [part.id for part in calls] == ["call-a", "call-b"]
    assert calls[0].signature == "sig-a"
    assert [part.call_id for part in results] == ["call-a", "call-b"]
    assert results[0].outcome is Outcome.SUCCESS
    assert "\"value\":41" in results[0].content
    assert results[1].outcome is Outcome.OUTCOME_UNKNOWN
    assert "inspect current state" in results[1].content


def test_legacy_rows_rehydrate_with_stable_synthetic_ids():
    completion = SimpleNamespace(id="legacy-completion", role="system")
    block = SimpleNamespace(
        tool_execution_id="legacy-tool-id",
        plan_decision_id="legacy-decision",
        agent_execution_id="legacy-execution",
        block_index=1,
    )
    tool = SimpleNamespace(
        id="legacy-tool-id",
        action_index=None,
        provider_call_id=None,
        provider_name=None,
        provider_signature=None,
        tool_name="inspect_data",
        arguments_json={"step_id": "old"},
        status="success",
        context_summary_json=None,
        result_json={"summary": "legacy result", "value": 17},
        result_summary="legacy result",
        error_message=None,
    )
    transcript, _ = build_durable_transcript(
        completions=[completion],
        blocks_by_completion={"legacy-completion": [block]},
        tool_exec_by_id={"legacy-tool-id": tool},
    )
    call = next(
        part for part in transcript.turns[0].parts if isinstance(part, ToolCallPart)
    )
    result = next(
        part for part in transcript.turns[1].parts if isinstance(part, ToolResultPart)
    )
    assert call.id == "legacy_call_legacy-tool-id"
    assert result.call_id == call.id
    assert "\"value\":17" in result.content


def test_blockless_intent_from_hard_kill_is_still_rehydrated():
    completion = SimpleNamespace(id="killed-completion", role="system")
    orphan = SimpleNamespace(
        id="orphan-tool",
        completion_id="killed-completion",
        agent_execution_id="killed-execution",
        plan_decision_id="killed-decision",
        action_index=0,
        provider_call_id="provider-orphan",
        provider_name="anthropic",
        provider_signature=None,
        tool_name="create_data",
        arguments_json={"description": "possibly created"},
        status="in_progress",
        context_summary_json=None,
        result_json=None,
        result_summary=None,
        error_message=None,
    )
    transcript, represented = build_durable_transcript(
        completions=[completion],
        blocks_by_completion={},
        tool_exec_by_id={"orphan-tool": orphan},
    )

    assert represented == {"orphan-tool"}
    result = next(
        part
        for turn in transcript.turns
        for part in turn.parts
        if isinstance(part, ToolResultPart)
    )
    assert result.call_id == "provider-orphan"
    assert result.outcome is Outcome.OUTCOME_UNKNOWN


def test_multiple_blockless_decisions_have_stable_chronological_order():
    """Database return order must not reorder consecutive interrupted calls."""
    completion = SimpleNamespace(id="killed-completion", role="system")
    started = datetime(2026, 1, 1, 12, 0, 0)

    def _orphan(execution_id: str, decision_id: str, offset: int):
        return SimpleNamespace(
            id=execution_id,
            completion_id="killed-completion",
            agent_execution_id="killed-execution",
            plan_decision_id=decision_id,
            action_index=0,
            provider_call_id=f"provider-{execution_id}",
            provider_name="anthropic",
            provider_signature=None,
            tool_name="inspect_data",
            arguments_json={"step_id": execution_id},
            status="in_progress",
            context_summary_json=None,
            result_json=None,
            result_summary=None,
            error_message=None,
            started_at=started + timedelta(seconds=offset),
        )

    later = _orphan("later", "decision-later", 2)
    earlier = _orphan("earlier", "decision-earlier", 1)
    transcript, _ = build_durable_transcript(
        completions=[completion],
        blocks_by_completion={},
        # Deliberately reverse chronological insertion order.
        tool_exec_by_id={"later": later, "earlier": earlier},
    )

    calls = [
        part
        for turn in transcript.turns
        for part in turn.parts
        if isinstance(part, ToolCallPart)
    ]
    assert [part.id for part in calls] == ["provider-earlier", "provider-later"]
