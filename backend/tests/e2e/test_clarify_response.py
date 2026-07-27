"""Persistence contract for POST .../tool_executions/{id}/clarify_response.

The endpoint stores the user's clarify-form answers on the tool execution's
result_json so the form can rehydrate after reload / on another device.
Since multi-pick support, a ``selected_chips`` entry is either a string
(single-pick question) or a list of strings (``multi_select`` question) —
the service must persist both shapes and drop non-string junk instead of
storing it.

Seeds the completion -> agent_execution -> tool_execution graph directly
(the API cannot produce a pending clarify without an LLM in the loop).
"""
import asyncio
import uuid

import pytest
from fastapi import HTTPException

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.report import Report
from app.models.completion import Completion
from app.models.agent_execution import AgentExecution
from app.models.tool_execution import ToolExecution
from app.services.completion_service import CompletionService


async def _seed(tool_name: str = "clarify", questions=None):
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Clarify Org {suffix}")
        db.add(org)
        await db.flush()

        user = User(
            name="Clarify User",
            email=f"clarify-{suffix}@example.com",
            hashed_password="x",
            is_active=True,
            is_superuser=False,
            is_verified=True,
        )
        db.add(user)
        await db.flush()

        report = Report(
            title=f"Clarify Report {suffix}",
            slug=f"clarify-report-{suffix}",
            status="draft",
            user_id=user.id,
            organization_id=org.id,
        )
        db.add(report)
        await db.flush()

        completion = Completion(
            prompt={"content": "build a dashboard"},
            completion={"content": ""},
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
            tool_name=tool_name,
            status="success",
            success=True,
            arguments_json={"questions": questions or []},
            result_json={"status": "awaiting_response"},
        )
        db.add(te)
        await db.flush()
        await db.commit()

        return {
            "org": org,
            "user": user,
            "completion_id": str(completion.id),
            "tool_execution_id": str(te.id),
        }


async def _submit(seeded, body):
    svc = CompletionService()
    async with async_session_maker() as db:
        return await svc.submit_clarify_response(
            db,
            seeded["completion_id"],
            seeded["tool_execution_id"],
            body,
            current_user=seeded["user"],
            organization=seeded["org"],
        )


@pytest.mark.e2e
def test_persists_single_and_multi_pick_shapes():
    async def scenario():
        seeded = await _seed(questions=[
            {"text": "Which metrics?", "options": ["Revenue", "Orders", "Other…"], "multi_select": True},
            {"text": "Which range?", "options": ["7d", "30d"]},
            {"text": "Chart title?"},
        ])
        res = await _submit(seeded, {
            "selected_chips": [["Revenue", "Orders"], "30d", ""],
            "other_texts": ["", "", ""],
            "free_texts": ["", "", "Q3 overview"],
        })
        assert res["ok"] is True
        rj = res["result_json"]
        assert rj["status"] == "answered"
        ur = rj["user_response"]
        assert ur["selected_chips"] == [["Revenue", "Orders"], "30d", ""]
        assert ur["free_texts"][2] == "Q3 overview"

        # Round-trip: the stored shape is what a fresh read sees.
        async with async_session_maker() as db:
            row = await db.get(ToolExecution, seeded["tool_execution_id"])
            assert row.result_json["user_response"]["selected_chips"][0] == ["Revenue", "Orders"]

    asyncio.run(scenario())


@pytest.mark.e2e
def test_non_string_junk_is_dropped_not_stored():
    async def scenario():
        seeded = await _seed()
        res = await _submit(seeded, {
            "selected_chips": [["Revenue", 7, None, "Orders"], {"evil": 1}, 42],
            "other_texts": [None, 3, "note"],
            "free_texts": [{"a": 1}],
        })
        ur = res["result_json"]["user_response"]
        assert ur["selected_chips"] == [["Revenue", "Orders"], "", ""]
        assert ur["other_texts"] == ["", "", "note"]
        assert ur["free_texts"] == [""]

    asyncio.run(scenario())


@pytest.mark.e2e
def test_rejects_non_clarify_tool_execution():
    async def scenario():
        seeded = await _seed(tool_name="create_data")
        with pytest.raises(HTTPException) as exc:
            await _submit(seeded, {"selected_chips": ["x"]})
        assert exc.value.status_code == 400

    asyncio.run(scenario())
