"""A REAL agent execution for tests that stage AI draft builds.

instruction_builds.agent_execution_id is a foreign key. SQLite (the local
test default) does not enforce it, so tests that passed a made-up
"exec-abc123" string went green locally — and failed on the Postgres CI leg
with ForeignKeyViolationError. Any test binding a build to an execution must
create the real chain: Report -> Completion -> AgentExecution.
"""
import uuid


async def make_agent_execution(org_id, user_id) -> str:
    """Create Report -> Completion -> AgentExecution; return the execution id."""
    from app.dependencies import async_session_maker
    from app.models.report import Report
    from app.models.completion import Completion
    from app.models.agent_execution import AgentExecution

    async with async_session_maker() as db:
        report = Report(
            title="Training session",
            slug=f"exec-{uuid.uuid4().hex[:8]}",
            organization_id=str(org_id),
            user_id=str(user_id),
        )
        db.add(report)
        await db.commit()
        await db.refresh(report)

        completion = Completion(
            prompt={"content": "teach"}, completion={"content": "noted"},
            report_id=str(report.id), role="system", status="success",
            turn_index=0, user_id=str(user_id),
        )
        db.add(completion)
        await db.commit()
        await db.refresh(completion)

        execution = AgentExecution(
            report_id=str(report.id),
            completion_id=str(completion.id),
            organization_id=str(org_id),
            user_id=str(user_id),
            status="success",
        )
        db.add(execution)
        await db.commit()
        await db.refresh(execution)
        return str(execution.id)
