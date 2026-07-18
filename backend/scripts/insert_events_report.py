#!/usr/bin/env python3
"""Insert a demo report with a conversation + silent session events into the
running stack's DB (db/agent.db), so the report page renders the event strips.

    cd backend && uv run python scripts/insert_events_report.py <admin_user_id> <org_id>

Prints the created report id.
"""
import asyncio
import sys
import uuid
from datetime import datetime, timedelta

import tests.conftest  # noqa: F401 — registers the full mapped model graph
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.completion import Completion
from app.models.report import Report


DB_URL = "sqlite+aiosqlite:///db/agent.db"
BASE = datetime.utcnow() - timedelta(minutes=30)


async def _turn(db, report, user_id, role, content, minute):
    c = Completion(
        prompt={"content": content} if role == "user" else {"content": ""},
        completion={"content": content} if role == "system" else {},
        status="success", model="demo", role=role, message_type="ai_completion",
        turn_index=minute, report_id=str(report.id),
        user_id=str(user_id) if role == "user" else None,
    )
    db.add(c)
    await db.flush()
    c.created_at = BASE + timedelta(minutes=minute)
    await db.commit()
    return c


async def main(user_id: str, org_id: str):
    from app.services.session_event_service import SessionEventService
    from app.ai.context import session_events as SE

    engine = create_async_engine(DB_URL)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as db:
        report = Report(
            title="Revenue analysis (session events demo)",
            slug=f"events-demo-{uuid.uuid4().hex[:8]}",
            user_id=str(user_id), organization_id=str(org_id),
            status="draft",
        )
        db.add(report)
        await db.commit()

        async def ev(kind, minute, meta):
            e = await SessionEventService.emit(db, report=report, user_id=str(user_id), kind=kind, meta=meta)
            e.created_at = BASE + timedelta(minutes=minute)
            await db.commit()

        await _turn(db, report, user_id, "user", "What was revenue by month in 2025?", 0)
        await _turn(db, report, user_id, "system",
                    "Here is monthly revenue for 2025 — 12 rows across month and revenue.", 1)
        await ev(SE.FEEDBACK_GIVEN, 2, {"direction": -1, "message": "used calendar year, we run on fiscal"})
        await ev(SE.LLM_CHANGED, 3, {"from": "claude-sonnet", "to": "claude-opus-4-8"})
        await _turn(db, report, user_id, "user", "redo it on our fiscal calendar (Feb–Jan)", 4)
        await _turn(db, report, user_id, "system",
                    "Recomputed on the fiscal calendar — actuals now aligned to fiscal months.", 5)
        await ev(SE.FILE_UPLOADED, 6, {"filename": "targets_2025.xlsx", "file_id": "file_abc"})
        await ev(SE.AGENT_SCOPE_CHANGED, 7, {"added": ["Snowflake PROD"], "kind": "data_source"})
        await _turn(db, report, user_id, "user", "compare actuals vs the targets I just uploaded", 8)
        await _turn(db, report, user_id, "system",
                    "Actuals vs targets by fiscal month — three of twelve months are below target.", 9)
        await ev(SE.ARTIFACT_SHARED, 10, {"title": "Revenue vs Targets", "shared_with": ["finance-team"]})

        print(str(report.id))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
