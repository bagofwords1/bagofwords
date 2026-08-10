"""
Regression guard for what GET /api/c/{token} (public shared conversation) serves.

A report accumulates rows that are NOT transcript content: silent session events
(role='event' — the ledger of out-of-band UI actions, see
app.ai.context.session_events), machine trigger entries (role='external') and
the hidden synthetic prompt the agent answers for a webhook/scheduled run
(role='user' with webhook_id/trigger_source set).

The share page renders none of them — they carry no prompt and no blocks in the
sanitized payload — so each one used to arrive as a completion the page drew as
an empty assistant bubble AND consumed a slot in the `limit` page. A report with
a busy event ledger (24 events / 28 completions in the report that prompted this
guard) paged in a wall of blank bubbles and almost nothing readable.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/test_public_conv.db \
      .venv/bin/python -m pytest tests/e2e/test_public_conversation_filtering.py -v -s
"""
import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.report import Report
from app.models.completion import Completion
from app.models.agent_execution import AgentExecution
from app.models.plan_decision import PlanDecision
from app.models.tool_execution import ToolExecution
from app.models.completion_block import CompletionBlock
from app.services.report_service import ReportService


N_TURNS = 6          # 6 user + 6 system = 12 renderable completions
EVENTS_PER_TURN = 3  # silent noise interleaved between every turn


def _run(coro):
    return asyncio.run(coro)


async def _seed():
    """One report whose timeline is mostly noise: every turn is followed by a
    few silent events, an external machine entry and a hidden trigger prompt."""
    suffix = uuid.uuid4().hex[:8]
    base = datetime(2026, 1, 1, 10, 0, 0)
    token = f"tok-{uuid.uuid4().hex}"

    async with async_session_maker() as db:
        org = Organization(name=f"Conv Org {suffix}")
        db.add(org)
        await db.flush()

        user = User(
            name="Conv User",
            email=f"conv-{suffix}@example.com",
            hashed_password="x",
            is_active=True,
            is_superuser=False,
            is_verified=True,
        )
        db.add(user)
        await db.flush()

        report = Report(
            title=f"Conv Report {suffix}",
            slug=f"conv-report-{suffix}",
            status="draft",
            user_id=user.id,
            organization_id=org.id,
            conversation_visibility="public",
            conversation_share_token=token,
        )
        db.add(report)
        await db.flush()

        renderable, hidden = [], []
        t = base
        for i in range(N_TURNS):
            t += timedelta(minutes=5)
            u = Completion(
                prompt={"content": f"q{i}"}, completion=None, status="success",
                role="user", report_id=report.id, user_id=user.id, turn_index=i,
            )
            u.created_at = u.updated_at = t
            s = Completion(
                prompt=None, completion={"content": f"a{i}"}, status="success",
                role="system", report_id=report.id, turn_index=i,
            )
            s.created_at = s.updated_at = t + timedelta(seconds=2)
            db.add_all([u, s])
            await db.flush()
            renderable.extend([str(u.id), str(s.id)])

            for j in range(EVENTS_PER_TURN):
                ev = Completion(
                    prompt={"content": f"something happened {i}.{j}"},
                    completion=None, status="success", role="event",
                    message_type="llm_changed", report_id=report.id, turn_index=i,
                )
                ev.created_at = ev.updated_at = t + timedelta(seconds=10 + j)
                db.add(ev)
                await db.flush()
                hidden.append(str(ev.id))

            ext = Completion(
                prompt={"content": f"webhook fired {i}"}, completion=None,
                status="success", role="external", report_id=report.id, turn_index=i,
            )
            ext.created_at = ext.updated_at = t + timedelta(seconds=20)
            trig = Completion(
                prompt={"content": f"internal trigger {i}"}, completion=None,
                status="success", role="user", trigger_source="scheduled",
                report_id=report.id, turn_index=i,
            )
            trig.created_at = trig.updated_at = t + timedelta(seconds=21)
            db.add_all([ext, trig])
            await db.flush()
            hidden.extend([str(ext.id), str(trig.id)])

        await db.commit()
        return {"token": token, "renderable": renderable, "hidden": set(hidden)}


async def _walk_pages(svc, token, limit, max_pages=50):
    served, pages = [], 0
    before = None
    async with async_session_maker() as db:
        while True:
            resp = await svc.get_public_conversation(db, token, limit=limit, before=before)
            pages += 1
            assert len(resp["completions"]) <= limit
            served.append(resp["completions"])
            if not resp["has_more"] or pages >= max_pages:
                return served, pages
            assert resp["next_before"], "has_more without a next_before cursor"
            before = str(resp["next_before"])


@pytest.mark.e2e
def test_public_conversation_serves_only_renderable_messages():
    """Events / external entries / hidden triggers never reach the share page."""
    async def scenario():
        svc = ReportService()
        seeded = await _seed()

        page_list, _ = await _walk_pages(svc, seeded["token"], limit=10)
        # Pages walk backwards (newest page first, each `before` cursor older),
        # while each page is itself chronological — reversing the page order
        # reassembles the transcript as the reader scrolls it.
        served = [c for page in reversed(page_list) for c in page]
        served_ids = [str(c["id"]) for c in served]

        leaked = seeded["hidden"] & set(served_ids)
        assert not leaked, f"{len(leaked)} non-message rows served to the share page"
        assert {c["role"] for c in served} == {"user", "system"}
        # Chronological, every real message, exactly once.
        assert served_ids == seeded["renderable"]
        # Every user row still carries the prompt the page renders.
        for c in served:
            if c["role"] == "user":
                assert (c["prompt"] or {}).get("content")

    _run(scenario())


@pytest.mark.e2e
def test_page_size_counts_only_renderable_messages():
    """`limit` is a count of visible messages, not of raw rows — otherwise the
    first page of a busy report is all noise and the reader sees nothing."""
    async def scenario():
        svc = ReportService()
        seeded = await _seed()

        page_list, page_count = await _walk_pages(svc, seeded["token"], limit=4)
        # 12 renderable rows / 4 per page. With the noise counted (48 rows) this
        # took 12 pages and most of them came back visually blank.
        assert page_count == len(seeded["renderable"]) // 4
        assert all(len(p) == 4 for p in page_list)

    _run(scenario())


async def _seed_with_blocks():
    """One shared report with a single answered turn whose system completion
    carries a main-loop step and a knowledge-harness step."""
    suffix = uuid.uuid4().hex[:8]
    token = f"tok-{uuid.uuid4().hex}"
    t = datetime(2026, 2, 1, 9, 0, 0)

    async with async_session_maker() as db:
        org = Organization(name=f"Blocks Org {suffix}")
        db.add(org)
        await db.flush()
        user = User(
            name="Blocks User", email=f"blocks-{suffix}@example.com",
            hashed_password="x", is_active=True, is_superuser=False, is_verified=True,
        )
        db.add(user)
        await db.flush()
        report = Report(
            title=f"Blocks Report {suffix}", slug=f"blocks-report-{suffix}",
            status="draft", user_id=user.id, organization_id=org.id,
            conversation_visibility="public", conversation_share_token=token,
        )
        db.add(report)
        await db.flush()

        u = Completion(
            prompt={"content": "which tables matter?"}, status="success",
            role="user", report_id=report.id, user_id=user.id, turn_index=0,
        )
        u.created_at = u.updated_at = t
        s = Completion(
            prompt=None, completion=None, status="success",
            role="system", report_id=report.id, turn_index=0,
        )
        s.created_at = s.updated_at = t + timedelta(seconds=2)
        db.add_all([u, s])
        await db.flush()

        ae = AgentExecution(completion_id=s.id, organization_id=org.id,
                            report_id=report.id, status="success")
        db.add(ae)
        await db.flush()

        def _block(idx, phase, tool, args, result):
            pd = PlanDecision(agent_execution_id=ae.id, seq=idx, loop_index=idx,
                              plan_type="action", phase=phase)
            te = ToolExecution(agent_execution_id=ae.id, tool_name=tool,
                               arguments_json=args, status="success", success=True,
                               result_json=result)
            db.add_all([pd, te])
            return pd, te

        pd_main, te_main = _block(
            0, None, "describe_tables", {"title": "Reading orders"},
            {"success": True, "widget_data": [{"row": i} for i in range(50)]},
        )
        pd_harness, te_harness = _block(
            1, "knowledge_harness", "create_instruction",
            {"title": "Order status codes", "text": "Status 3 means shipped."},
            {"success": True, "instruction_id": "i-1", "build_id": "b-1",
             "title": "Order status codes"},
        )
        await db.flush()

        for idx, (pd, te) in enumerate(((pd_main, te_main), (pd_harness, te_harness))):
            db.add(CompletionBlock(
                completion_id=s.id, agent_execution_id=ae.id, source_type="tool",
                plan_decision_id=pd.id, tool_execution_id=te.id,
                block_index=idx, title=te.tool_name, status="completed",
            ))
        await db.commit()
        return token


@pytest.mark.e2e
def test_blocks_carry_phase_and_tool_arguments():
    """The tool cards paint from arguments_json, and the Knowledge card is split
    out of the step stream by `phase` — neither reached the share page before."""
    async def scenario():
        svc = ReportService()
        token = await _seed_with_blocks()

        async with async_session_maker() as db:
            resp = await svc.get_public_conversation(db, token, limit=10)

        system = [c for c in resp["completions"] if c["role"] == "system"]
        assert len(system) == 1
        blocks = system[0]["completion_blocks"]
        assert len(blocks) == 2
        by_tool = {b["tool_execution"]["tool_name"]: b for b in blocks}

        main = by_tool["describe_tables"]
        assert main["phase"] is None
        # The step-group ticker labels itself from arguments_json.title.
        assert main["tool_execution"]["arguments_json"]["title"] == "Reading orders"
        # Whole result sets never ship on a page that renders every block.
        assert "widget_data" not in main["tool_execution"]["result_json"]

        harness = by_tool["create_instruction"]
        assert harness["phase"] == "knowledge_harness"
        # The Knowledge card derives its change list from these two fields.
        assert harness["tool_execution"]["arguments_json"]["text"]
        assert harness["tool_execution"]["result_json"]["instruction_id"] == "i-1"

    _run(scenario())
