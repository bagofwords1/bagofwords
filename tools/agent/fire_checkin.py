#!/usr/bin/env python3
"""Force an agent check-in due NOW (debug path for the live feedback loop).

Runs the real fire path — setting, access, caps, live-run guard, the judge on
the org's small model, the machine turn on the org's default model, notify —
in this process against the same database, without waiting in real time.

Two clocks:
  (default)  --at-due   fire as the scheduler would: the check-in's clock is its
                        due_at (the judge sees "now = due time"; judged_at and the
                        cap windows use it). Queries and the agent run are real.
  --now                 fire at the real current time; only the Mon–Fri
                        09:00–18:00 working-window re-arm is bypassed.

    cd backend && TEST_DATABASE_URL=sqlite:///db/agent.db TESTING=true \\
        uv run python ../tools/agent/fire_checkin.py <checkin_id> | --latest [--report <id>] [--now]

Prints the final status and the row as JSON.
"""
import argparse
import asyncio
import json
import sys


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("checkin_id", nargs="?")
    ap.add_argument("--latest", action="store_true", help="the newest planned check-in")
    ap.add_argument("--report", help="restrict --latest to this report")
    ap.add_argument("--now", action="store_true", help="fire at the real current time (default: at due_at)")
    args = ap.parse_args()

    import main as _app  # noqa: F401  registers every mapper
    from datetime import datetime
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.agent_checkin import AgentCheckin, STATUS_PLANNED
    from app.services.checkin_service import checkin_service

    async with async_session_maker() as db:
        cid = args.checkin_id
        if args.latest:
            q = select(AgentCheckin).where(AgentCheckin.status == STATUS_PLANNED)
            if args.report:
                q = q.where(AgentCheckin.report_id == args.report)
            row = (await db.execute(q.order_by(AgentCheckin.created_at.desc()).limit(1))).scalars().first()
            if row is None:
                print("no planned check-in", file=sys.stderr)
                return 1
            cid = row.id
        if not cid:
            ap.error("checkin_id or --latest required")
        row = await db.get(AgentCheckin, cid)
        if args.now:
            row.due_at = datetime.utcnow()  # record that it was forced due now
            await db.commit()
        fire_at = row.due_at
        # Drop the pending scheduler job so the real fire can't run it again.
        # This process's scheduler isn't running, so go to the shared job store
        # directly (a stale job would be a harmless no-op anyway: only a
        # 'planned' row can be claimed).
        try:
            from app.core.scheduler import jobstore
            jobstore.remove_job(row.job_id)
        except Exception:
            pass
        status = await checkin_service.fire(db, cid, now=fire_at, ignore_working_window=args.now)
        await db.refresh(row)
        # Let fire-and-forget writes (LLM usage records) land before exit.
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.wait(pending, timeout=15)
        await asyncio.sleep(1.0)
        print(json.dumps({
            "status": status, "id": row.id, "report_id": row.report_id,
            "status_reason": row.status_reason, "judge_decision": row.judge_decision,
            "judge_reason": row.judge_reason, "judge_focus": row.judge_focus,
            "notified": row.notified, "notify_subject": row.notify_subject,
            "run_completion_id": row.run_completion_id,
        }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
