#!/usr/bin/env python3
"""Seed a report whose user turns include a very long prompt (pasted spec),
to verify the report page clamps it behind "Read more".

    cd backend && TESTING=true ENVIRONMENT=production \
      TEST_DATABASE_URL=sqlite:///db/agent.db \
      uv run python ../tools/agent/seed_long_prompt_report.py

Prints JSON {report_id}. Requires the stack (backend :8000) and seed_org.py.
"""
import asyncio, json, os, sys
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seed_shared_report as base

LONG_PROMPT = "Please review this churn analysis spec and build the dashboard it describes.\n\n" + "\n\n".join(
    f"Section {i}: For every region, compute monthly active customers, churned customers and net revenue "
    f"retention. Exclude test accounts, treat reactivations within 30 days as retained, and break the "
    f"result down by plan tier (free, pro, enterprise). Flag any month where churn exceeds 4%."
    for i in range(1, 31)
)
SHORT_PROMPT = "Thanks — now show the same view for the last quarter only."


async def seed(report_id):
    import main  # noqa
    from app.dependencies import async_session_maker
    from app.models.report import Report
    from app.models.completion import Completion
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        t0 = datetime.utcnow() - timedelta(hours=1)
        for i, text in enumerate([LONG_PROMPT, SHORT_PROMPT]):
            ts = t0 + timedelta(minutes=10 * i)
            u = Completion(report_id=report_id, user_id=report.user_id, role="user", message_type="table",
                           prompt={"content": text}, completion={}, status="success", created_at=ts, turn_index=i)
            db.add(u); await db.flush()
            db.add(Completion(report_id=report_id, role="system", parent_id=u.id, prompt=None,
                              completion={"content": "Done — here is the summary."}, status="success",
                              created_at=ts + timedelta(seconds=5), turn_index=i))
        await db.commit()


def main():
    _, _, report = base.api_setup()
    asyncio.run(seed(report["id"]))
    print(json.dumps({"report_id": report["id"]}))


if __name__ == "__main__":
    main()
