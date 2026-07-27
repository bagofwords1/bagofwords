#!/usr/bin/env python3
"""Generate sample MessageContextBuilder output showing silent session events.

Builds a realistic conversation with interleaved events and dumps the exact
LLM-facing context (both the string builder and the object builder's render())
to text files, plus a compaction sample proving durable events fold while
ephemeral ones drop.

    cd backend && BOW_DATABASE_URL="sqlite:///db/app.db" \
        uv run python scripts/gen_session_event_samples.py <out_dir>
"""
import asyncio
import re
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Importing the test conftest registers the full mapped model graph (same setup
# the unit tests use), so create_all resolves every FK target table.
import tests.conftest  # noqa: F401

from app.models.base import Base
from app.models.completion import Completion
from app.models.organization import Organization
from app.models.report import Report
from app.models.user import User
from app.ai.context.builders.message_context_builder import MessageContextBuilder
from app.services.session_event_service import SessionEventService
from app.ai.context import session_events as SE

BASE = datetime(2026, 1, 1, 9, 0)


async def _turn(db, report, user, role, content, minute, message_type="ai_completion"):
    c = Completion(
        prompt={"content": content} if role == "user" else {"content": ""},
        completion={"content": content} if role == "system" else {},
        status="success", model="test", role=role, message_type=message_type,
        turn_index=minute, report_id=str(report.id),
        user_id=str(user.id) if role == "user" else None,
    )
    db.add(c)
    await db.flush()
    c.created_at = BASE + timedelta(minutes=minute)
    await db.commit()
    return c


async def _event(db, report, user, event_kind, minute, meta=None):
    ev = await SessionEventService.emit(db, report=report, user=user, kind=event_kind, meta=meta or {})
    ev.created_at = BASE + timedelta(minutes=minute)
    await db.commit()
    return ev


async def main(out_dir: Path):
    Completion.__table__.c.sigkill.nullable = True
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as db:
        user = User(name="Dana", email=f"dana-{uuid.uuid4()}@x.dev", hashed_password="x")
        db.add(user)
        await db.flush()
        org = Organization(name="Acme")
        db.add(org)
        await db.flush()
        report = Report(title="Revenue analysis", slug=f"r-{uuid.uuid4()}",
                        user_id=str(user.id), organization_id=str(org.id))
        db.add(report)
        await db.commit()
        org = (await db.execute(select(Organization).where(Organization.id == str(org.id)))).unique().scalar_one()

        # A realistic session: turns interleaved with out-of-band UI actions.
        await _turn(db, report, user, "user", "What was revenue by month in 2025?", 0)
        await _turn(db, report, user, "system",
                    "Here is monthly revenue for 2025. Tool: create_data (success) - 12 rows × 2 cols; cols: month, revenue", 1)
        await _event(db, report, user, SE.FEEDBACK_GIVEN, 2, {"direction": -1, "message": "used calendar year, we run on fiscal"})
        await _event(db, report, user, SE.LLM_CHANGED, 3, {"from": "claude-sonnet", "to": "claude-opus-4-8"})
        await _turn(db, report, user, "user", "redo it on our fiscal calendar (Feb–Jan)", 4)
        await _turn(db, report, user, "system",
                    "Recomputed on the fiscal calendar. Tool: create_data (success) - 12 rows × 2 cols; cols: fiscal_month, revenue", 5)
        await _event(db, report, user, SE.FEEDBACK_CHANGED, 6, {"direction": 1, "message": "perfect"})
        await _event(db, report, user, SE.FILE_UPLOADED, 7, {"filename": "targets_2025.xlsx", "file_id": "file_abc"})
        await _event(db, report, user, SE.AGENT_SCOPE_CHANGED, 8, {"added": ["Snowflake PROD"], "kind": "data_source"})
        await _turn(db, report, user, "user", "compare actuals vs the targets I just uploaded", 9)
        await _turn(db, report, user, "system",
                    "Actuals vs targets by fiscal month. Tool: create_data (success) - 12 rows × 3 cols; viz_id: 7f3a1c2b", 10)
        await _event(db, report, user, SE.ARTIFACT_SHARED, 11, {"title": "Revenue vs Targets", "shared_with": ["finance-team"]})
        await _event(db, report, user, SE.INSTRUCTION_REJECTED, 12,
                     {"title": "Always assume calendar year", "snippet": "rejected: we use a fiscal calendar", "source": "agent_suggested"})
        await _turn(db, report, user, "user", "great, schedule this to refresh every Monday", 13)
        await _turn(db, report, user, "system", "Scheduled a weekly refresh. Tool: create_scheduled_task (success)", 14)

        builder = MessageContextBuilder(db, org, report)

        # 1) String builder — what the planner sees inline.
        ctx = await builder.build_context(max_messages=40)
        (out_dir / "01_context_build_context.txt").write_text(
            "# MessageContextBuilder.build_context() — LLM-facing conversation context\n"
            "# Silent session events (role='event') interleaved by timestamp.\n"
            "# Note: llm_changed renders in context but is UI-hidden; export/audit\n"
            "#       kinds never render at all.\n\n" + ctx + "\n"
        )

        # 2) Object builder — rendered <conversation> section (recent-full + minified).
        section = await builder.build(max_messages=40)
        (out_dir / "02_context_conversation_section.txt").write_text(
            "# MessageContextBuilder.build().render() — <conversation> section\n"
            "# Same events as objects (role='event' MessageItems).\n\n" + section.render() + "\n"
        )

        # 3) Compaction fold digest — durable events survive, ephemeral drop.
        rows = (await db.execute(
            select(Completion).where(Completion.report_id == str(report.id))
            .order_by(Completion.created_at.asc())
        )).scalars().all()
        fold_ids = [str(r.id) for r in rows]  # fold the whole session
        fold_section = await builder.build(completion_ids=fold_ids)
        (out_dir / "03_compaction_fold_digest.txt").write_text(
            "# MessageContextBuilder.build(completion_ids=...) — the digest handed to\n"
            "# the summarizer when these turns are folded into the rolling summary.\n"
            "# DURABLE events (feedback, instruction_rejected) are kept; EPHEMERAL\n"
            "# events (llm_changed, file_uploaded, agent_scope_changed,\n"
            "# artifact_shared) are DROPPED — their state already lives in other\n"
            "# context sections, so they must not spend summary budget.\n\n"
            + fold_section.render() + "\n"
        )

        # Small manifest of the policy applied.
        lines = ["# Event kinds emitted in this sample and their policy\n",
                 f"# {'kind':28} llm  ui   durable"]
        for kind in [SE.FEEDBACK_GIVEN, SE.FEEDBACK_CHANGED, SE.LLM_CHANGED, SE.FILE_UPLOADED,
                     SE.AGENT_SCOPE_CHANGED, SE.ARTIFACT_SHARED, SE.INSTRUCTION_REJECTED, SE.EXPORT_DOWNLOADED]:
            lines.append(
                f"  {kind:28} "
                f"{'yes' if SE.is_event_kind_llm_visible(kind) else 'no ':4} "
                f"{'yes' if SE.is_event_kind_ui_visible(kind) else 'no ':4} "
                f"{'yes' if SE.is_event_kind_durable(kind) else 'no'}"
            )
        (out_dir / "00_policy_manifest.txt").write_text("\n".join(lines) + "\n")

    await engine.dispose()
    print(f"wrote samples to {out_dir}")


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    out.mkdir(parents=True, exist_ok=True)
    asyncio.run(main(out))
