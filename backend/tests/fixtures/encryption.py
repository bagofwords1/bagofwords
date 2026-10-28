"""Parent rows for tests that insert directly into the encrypted tables.

PostgreSQL enforces the foreign keys SQLite ignores, so a test row must hang
off a real organization → user → report → widget/step → completion → agent
execution chain. No public API creates those without an LLM run, which is why
they are seeded with Core inserts here.
"""
from __future__ import annotations

import uuid


async def seed_encryption_graph(db) -> dict:
    """Seed one chain and return its ids keyed org/user/report/widget/step/
    completion/execution."""
    from app.models.agent_execution import AgentExecution as AE
    from app.models.completion import Completion
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.step import Step
    from app.models.user import User
    from app.models.widget import Widget

    g = {k: str(uuid.uuid4()) for k in ("org", "user", "report", "widget", "step", "completion", "execution")}
    await db.execute(Organization.__table__.insert().values(id=g["org"], name="org-" + g["org"]))
    await db.execute(User.__table__.insert().values(id=g["user"], name="u", email=f"{g['user']}@x.test", hashed_password="x"))
    await db.execute(Report.__table__.insert().values(
        id=g["report"], slug=g["report"], title="r", user_id=g["user"], organization_id=g["org"],
    ))
    await db.execute(Widget.__table__.insert().values(id=g["widget"], slug=g["widget"], report_id=g["report"]))
    await db.execute(Step.__table__.insert().values(
        id=g["step"], title="t", slug=g["step"], status="success", prompt="", code="",
        data={}, description="", type="table", data_model={}, view={}, widget_id=g["widget"],
    ))
    await db.execute(Completion.__table__.insert().values(
        id=g["completion"], report_id=g["report"], prompt={"content": ""}, completion={"content": ""},
        status="success", model="m", turn_index=0, feedback_score=0, message_type="ai_completion",
    ))
    await db.execute(AE.__table__.insert().values(id=g["execution"], completion_id=g["completion"]))
    await db.commit()
    return g


