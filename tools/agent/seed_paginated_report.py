#!/usr/bin/env python3
"""Seed a LONG report (many turns + one very long answer) with planted facts,
to verify that the planner's read_report tool can read a report progressively.

    cd backend && TESTING=true ENVIRONMENT=production TEST_DATABASE_URL=sqlite:///db/agent.db \
        uv run python ../tools/agent/seed_paginated_report.py --email admin@example.com

Planted facts (an agent must page / read past truncation to find them):
  - message 0 (very first user prompt): codename + churn threshold
  - one AI answer in the middle, ~6k chars in: the recommended discount

Prints JSON {report_id, title, total_messages, facts}.
"""
import argparse, asyncio, json, uuid
from datetime import datetime, timedelta

FIRST = ("Context for this whole analysis: the project codename is BLUEHERON and the "
         "agreed churn threshold for at-risk accounts is 17.4%. Start by listing the churn drivers.")
FILLER_Q = "Follow-up {i}: break churn down by segment {i} and tell me what changed."
FILLER_A = ("Segment {i}: churn moved slightly; retention cohorts look stable and support "
            "tickets are flat. Nothing notable this time.")
LONG_PARA = ("Cohort review paragraph {k}: monthly retention curves, plan mix, seat "
             "expansion and support load were reviewed for this cohort with no anomaly. ")
FACT_SENTENCE = " FINAL RECOMMENDATION: offer a 12.5% retention discount to tier GAMMA accounts. "


def long_answer():
    parts = [LONG_PARA.format(k=k) for k in range(40)]  # ~6.5k chars before the fact
    parts.append(FACT_SENTENCE)
    parts += [LONG_PARA.format(k=k) for k in range(40, 60)]
    return "".join(parts)


async def main(email, title, turns, long_at):
    import main as _app  # noqa: F401 — registers mappers
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.user import User
    from app.models.report import Report
    from app.models.completion import Completion
    from app.models.membership import Membership

    async with async_session_maker() as db:
        user = (await db.execute(select(User).where(User.email == email))).scalars().first()
        mem = (await db.execute(select(Membership).where(Membership.user_id == user.id))).scalars().first()
        rid = str(uuid.uuid4())
        db.add(Report(id=rid, title=title, slug=f"seed-{rid[:8]}", user_id=user.id,
                      organization_id=mem.organization_id, status="draft", mode="chat"))
        await db.flush()
        t0 = datetime.utcnow() - timedelta(days=2)
        long_text = long_answer()
        for i in range(turns):
            ts = t0 + timedelta(minutes=5 * i)
            q = FIRST if i == 0 else FILLER_Q.format(i=i)
            a = long_text if i == long_at else FILLER_A.format(i=i)
            u = Completion(report_id=rid, user_id=user.id, role="user", turn_index=i,
                           message_type="table", prompt={"content": q}, completion={},
                           status="success", created_at=ts)
            db.add(u); await db.flush()
            db.add(Completion(report_id=rid, role="system", parent_id=u.id, turn_index=i,
                              prompt={}, completion={"content": a}, status="success",
                              created_at=ts + timedelta(seconds=30)))
        await db.commit()
    print(json.dumps({
        "report_id": rid, "title": title, "total_messages": 2 * turns,
        "long_message_index": 2 * long_at + 1, "long_message_chars": len(long_text),
        "fact_char_offset": long_text.index("FINAL RECOMMENDATION"),
        "facts": {"codename": "BLUEHERON", "threshold": "17.4%", "discount": "12.5% / GAMMA"},
    }))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="admin@example.com")
    ap.add_argument("--title", default="Customer churn deep dive")
    ap.add_argument("--turns", type=int, default=45)
    ap.add_argument("--long-at", type=int, default=20)
    a = ap.parse_args()
    asyncio.run(main(a.email, a.title, a.turns, a.long_at))
