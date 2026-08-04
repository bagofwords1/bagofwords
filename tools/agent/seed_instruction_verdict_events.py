"""Seed a report carrying instruction_accepted / instruction_rejected strips.

Builds a conversation with two AI suggestions on one instruction, accepts one
and rejects the other, so the report timeline shows both verdict strips — the
UI surface for the events emitted by the review paths.

Run from backend/:
  BOW_DATABASE_URL='sqlite:///db/agent.db' uv run python \
    ../tools/agent/seed_instruction_verdict_events.py
"""
import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.realpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "backend")
))
os.environ.setdefault("BOW_DATABASE_URL", "sqlite:///db/agent.db")

import main  # noqa: F401

BASE = "Revenue excludes cancelled orders."


async def amain():
    from types import SimpleNamespace
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.user import User
    from app.models.report import Report
    from app.models.completion import Completion
    from app.models.agent_execution import AgentExecution
    from app.models.instruction import Instruction
    from app.schemas.instruction_schema import InstructionCreate
    from app.services.instruction_service import InstructionService
    from app.services.build_service import BuildService
    from app.ai.tools.implementations.edit_instruction import EditInstructionTool

    isvc, bsvc = InstructionService(), BuildService()

    async with async_session_maker() as db:
        org = (await db.execute(select(Organization).limit(1))).scalars().first()
        user = (await db.execute(
            select(User).where(User.email == "admin@example.com")
        )).scalars().first()

        report = Report(title="Training session", slug=f"verdicts-{uuid.uuid4().hex[:8]}",
                        organization_id=str(org.id), user_id=str(user.id))
        db.add(report)
        await db.commit()
        await db.refresh(report)

        instr = await isvc.create_instruction(
            db, InstructionCreate(text=BASE, category="general", status="published"),
            user, org, force_global=True,
        )
        iid = str(instr.id)

        async def turn(idx, ask, reply):
            db.add(Completion(
                prompt={"content": ask}, completion={"content": ""},
                report_id=str(report.id), role="user", status="success",
                turn_index=idx, user_id=str(user.id)))
            sysc = Completion(
                prompt={"content": ""}, completion={"content": reply},
                report_id=str(report.id), role="system", status="success",
                turn_index=idx, user_id=str(user.id))
            db.add(sysc)
            await db.commit()
            await db.refresh(sysc)
            ex = AgentExecution(report_id=str(report.id), completion_id=str(sysc.id),
                                organization_id=str(org.id), status="success")
            db.add(ex)
            await db.commit()
            await db.refresh(ex)
            return str(ex.id)

        async def suggest(exec_id, text):
            build = await bsvc.get_or_create_draft_build(
                db, org_id=str(org.id), source="ai", user_id=str(user.id),
                agent_execution_id=exec_id)
            ctx = {"db": db, "user": user, "organization": org, "mode": "training",
                   "training_build_id": str(build.id), "agent_execution_id": exec_id}
            async for evt in EditInstructionTool().run_stream(
                {"instruction_id": iid, "old_text": "", "text": text,
                 "evidence": "User confirmed the rule."}, ctx):
                if evt.type == "tool.error":
                    raise SystemExit(f"tool error: {evt.payload}")
            return str(build.id)

        ex_a = await turn(1, "refunds shouldn't count as revenue",
                          "Noted — I'll capture that as a rule.")
        build_a = await suggest(ex_a, "- Refunded orders are excluded from revenue as well.")

        ex_b = await turn(2, "also drop anything from the test account",
                          "Capturing that too.")
        build_b = await suggest(ex_b, "- Exclude all orders placed by the internal test account.")

        instruction = (await db.execute(
            select(Instruction).where(Instruction.id == iid))).scalars().first()

        # Accept suggestion A, reject suggestion B.
        _r, st = await isvc.accept_all_hunks(
            db, iid, against_main_version_id=None, organization=org,
            current_user=user, build_id=build_a)
        print("accept:", st)
        _r, st = await isvc.reject_all_hunks(
            db, iid, organization=org, current_user=user, build_id=build_b)
        print("reject:", st)

        events = (await db.execute(
            select(Completion).where(Completion.report_id == str(report.id),
                                     Completion.role == "event")
            .order_by(Completion.created_at))).scalars().all()
        for e in events:
            print(f"  [{e.message_type}] {e.prompt['content']}")
        print(f"\nreport: /reports/{report.id}")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
