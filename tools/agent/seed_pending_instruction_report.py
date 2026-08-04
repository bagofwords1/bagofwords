"""Seed a report whose chat contains several edit_instruction tool blocks with
PENDING suggestions — the shape that reportedly flickers on load.

Run from backend/:
  BOW_DATABASE_URL='sqlite:///db/agent.db' uv run python \
    ../tools/agent/seed_pending_instruction_report.py
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

BASE = (
    "Use this source for procurement-domain analysis covering purchases, "
    "suppliers, contracts and invoices.\n"
    "- Do not assume table names or invent identifiers.\n"
    "- Establish relationships only from explicit evidence."
)


async def amain():
    from types import SimpleNamespace
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.user import User
    from app.models.report import Report
    from app.models.completion import Completion
    from app.models.agent_execution import AgentExecution
    from app.schemas.instruction_schema import InstructionCreate
    from app.services.instruction_service import InstructionService
    from app.services.build_service import BuildService
    from app.ai.tools.implementations.edit_instruction import EditInstructionTool

    isvc, bsvc = InstructionService(), BuildService()

    async with async_session_maker() as db:
        org = (await db.execute(select(Organization).limit(1))).scalars().first()
        user = (await db.execute(
            select(User).where(User.email == "admin@example.com"))).scalars().first()

        report = Report(title="Procurement training", slug=f"flicker-{uuid.uuid4().hex[:8]}",
                        organization_id=str(org.id), user_id=str(user.id))
        db.add(report)
        await db.commit()
        await db.refresh(report)

        instr = await isvc.create_instruction(
            db, InstructionCreate(text=BASE, category="general", status="published",
                                  title="PROCUREMENT_OVERVIEW"),
            user, org, force_global=True,
        )
        iid = str(instr.id)

        edits = [
            ("only pdf files please",
             "- Do not assume table names or invent identifiers.",
             "- Do not assume table names or invent identifiers.\n- Process only PDF files."),
            ("also xlsx",
             "- Process only PDF files.",
             "- Process only PDF files and XLS/XLSX spreadsheets."),
            ("mention join cardinality",
             "- Establish relationships only from explicit evidence.",
             "- Establish relationships only from explicit evidence. Record join cardinality."),
        ]

        for idx, (ask, old_text, new_text) in enumerate(edits, start=1):
            db.add(Completion(prompt={"content": ask}, completion={"content": ""},
                              report_id=str(report.id), role="user", status="success",
                              turn_index=idx, user_id=str(user.id)))
            sysc = Completion(prompt={"content": ""},
                              completion={"content": f"Updated the instruction: {ask}."},
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

            build = await bsvc.get_or_create_draft_build(
                db, org_id=str(org.id), source="ai", user_id=str(user.id),
                agent_execution_id=str(ex.id))
            ctx = {"db": db, "user": user, "organization": org, "mode": "training",
                   "training_build_id": str(build.id), "agent_execution_id": str(ex.id)}
            tool_input = {"instruction_id": iid, "old_text": old_text, "text": new_text,
                          "evidence": f"User asked: {ask}"}
            out = None
            async for evt in EditInstructionTool().run_stream(tool_input, ctx):
                if evt.type == "tool.error":
                    raise SystemExit(f"edit error: {evt.payload}")
                if evt.type == "tool.end":
                    out = evt.payload["output"]
            print(f"  edit {idx}: success={out.get('success')} "
                  f"{out.get('rejected_reason') or ''} build={build.id}")

            # Persist a real ToolExecution + chat block so the report renders an
            # edit_instruction card — that surface mounts its own review panel.
            from app.project_manager import ProjectManager
            pm = ProjectManager()
            te = await pm.start_tool_execution(
                db, agent_execution=ex, plan_decision_id=None,
                tool_name="edit_instruction", tool_action=None,
                arguments_json=tool_input,
            )
            await pm.finish_tool_execution(
                db, tool_execution=te, status="success", success=True,
                result_summary=f"Edited instruction ({ask})", result_json=out,
            )
            # standalone block: upsert_block_for_tool needs a pre-existing
            # decision block (plan_decision_id) and silently no-ops without one.
            await pm.insert_standalone_tool_block(
                db, completion=sysc, agent_execution=ex, tool_execution=te,
                loop_index=1, title="Edit instruction", icon="📘")

        print(f"\nreport: /reports/{report.id}")
        print(f"instruction: {iid}")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
