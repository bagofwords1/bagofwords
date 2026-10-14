"""Deferred jobs that act AS a user re-check, at fire time, that the user may
still act in the org (Aikido 829403536 retest).

``test_wait_wake_deprovisioned.py`` pins this for wait wake-ups. The same rule
(``principal_can_act_in_org``) must hold for every other job that runs long
after the request that armed it, on the user's behalf:

- a scheduled report refresh (runs the report's queries as the scheduling
  user and mails subscribers),
- an eval run's wake-on-finish (an agent turn as the user who started it).

Each way of losing access stops the job: deactivated with the membership kept
(SCIM DELETE), removed from the members list. An active member still runs.

Seeding writes the deactivated / soft-removed states directly: SCIM is the only
API that produces the first, and that is not what these tests are about. The
LLM boundary (``run_machine_turn``), the query rerun itself, and the cross-
worker run claim are stubbed; the DB and the fire-time logic are real.
"""
import uuid
from datetime import datetime
from unittest.mock import patch

import pytest

from app.dependencies import async_session_maker
from app.models.eval import TestRun
from app.models.membership import Membership
from app.models.organization import Organization
from app.models.report import Report
from app.models.user import User

KINDS = [("active", True), ("deactivated", False), ("removed", False)]


async def _seed(kind: str, *, with_eval_run: bool = False):
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Deferred Org {suffix}")
        db.add(org)
        await db.flush()
        user = User(name="U", email=f"d-{suffix}@example.com", hashed_password="x",
                    is_active=kind != "deactivated", is_superuser=False, is_verified=True)
        db.add(user)
        await db.flush()
        db.add(Membership(user_id=user.id, organization_id=org.id, role="member",
                          deleted_at=datetime.utcnow() if kind == "removed" else None))
        report = Report(title="r", slug=f"r-{suffix}", user_id=user.id, organization_id=org.id)
        db.add(report)
        await db.flush()
        run_id = None
        if with_eval_run:
            run = TestRun(title="eval", suite_ids="s", status="success", summary_json={"passed": 1, "total": 1},
                          origin_report_id=report.id, origin_user_id=user.id, wake_on_finish=True)
            db.add(run)
            await db.flush()
            run_id = str(run.id)
        await db.commit()
        return {"report_id": str(report.id), "user_id": str(user.id), "org_id": str(org.id), "run_id": run_id}


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("kind,should_run", KINDS)
async def test_scheduled_report_refresh_runs_only_for_users_who_can_still_act(kind, should_run):
    from app.services import report_service as rs

    s = await _seed(kind)
    calls = []

    async def _fake_rerun(self, db, report_id, current_user, organization, **kw):
        calls.append(str(current_user.id))
        return {}

    with patch.object(rs, "claim_scheduled_run", lambda job_id: True), \
         patch.object(rs.ReportService, "rerun_report_steps", _fake_rerun):
        await rs.ReportService().scheduled_rerun_report_steps(s["report_id"], s["user_id"], s["org_id"])
    assert (calls == [s["user_id"]]) is should_run, calls


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("kind,should_run", KINDS)
async def test_eval_wake_runs_only_for_users_who_can_still_act(kind, should_run):
    from app.services.test_run_service import TestRunService

    s = await _seed(kind, with_eval_run=True)
    calls = []

    async def _fake_turn(*args, **kwargs):
        calls.append(str(kwargs["user"].id))

    with patch("app.services.machine_turn.run_machine_turn", _fake_turn):
        await TestRunService()._fire_eval_wake(s["run_id"])
    assert (calls == [s["user_id"]]) is should_run, calls
