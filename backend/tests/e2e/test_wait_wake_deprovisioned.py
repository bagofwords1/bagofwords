"""A scheduled ``wait`` wake-up must not run as a user who has since lost access.

The wait tool arms a one-shot job that later resumes the agent AS the user
(``run_wait_wake`` in ``app/services/wait_service.py``). That can be hours
after the request that armed it, so whether the user may still act is checked
when the job fires, not when it was armed. Each way a user loses access must
stop the wake:

- deactivated with the membership kept (what SCIM DELETE does)
- removed from the org's members
- a disabled service account

Service accounts are inactive by construction, so ``is_active`` alone must not
block a service account that is still bound to the org.

The LLM boundary (``run_machine_turn``) and the cross-worker run claim are
stubbed. Everything else, including the DB, is real.
"""
import uuid
from datetime import datetime
from unittest.mock import patch

import pytest

from app.dependencies import async_session_maker
from app.models.membership import Membership
from app.models.organization import Organization
from app.models.report import Report
from app.models.service_account import ServiceAccount
from app.models.user import User
from app.services import wait_service as ws


async def _seed(kind: str):
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Wait Org {suffix}")
        db.add(org)
        await db.flush()
        is_sa = kind.startswith("service_account")
        user = User(
            name="U", email=f"u-{suffix}@example.com", hashed_password="x",
            is_active=not is_sa, is_superuser=False, is_verified=True,
            is_service_account=is_sa,
        )
        db.add(user)
        await db.flush()
        if is_sa:
            db.add(ServiceAccount(
                organization_id=org.id, user_id=user.id, name=f"sa-{suffix}",
                disabled_at=datetime.utcnow() if kind == "service_account_disabled" else None,
            ))
        else:
            db.add(Membership(
                user_id=user.id, organization_id=org.id, role="member",
                deleted_at=datetime.utcnow() if kind == "removed" else None,
            ))
            if kind == "deactivated":
                user.is_active = False
        report = Report(title="r", slug=f"r-{suffix}", user_id=user.id, organization_id=org.id)
        db.add(report)
        await db.commit()
        return str(report.id), str(user.id), str(org.id)


async def _fire(report_id, user_id, org_id) -> bool:
    calls = []

    async def _fake_turn(*args, **kwargs):
        calls.append(kwargs)

    with patch.object(ws, "claim_scheduled_run", lambda job_id: True), \
         patch("app.services.machine_turn.run_machine_turn", _fake_turn):
        await ws.run_wait_wake(
            job_id=f"wait:{report_id}:{uuid.uuid4().hex[:6]}", report_id=report_id,
            user_id=user_id, organization_id=org_id, reason="check again",
        )
    return bool(calls)


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("kind,should_run", [
    ("active", True),
    ("service_account", True),
    ("deactivated", False),
    ("removed", False),
    ("service_account_disabled", False),
])
async def test_wait_wake_runs_only_for_principals_who_can_still_act(kind, should_run):
    report_id, user_id, org_id = await _seed(kind)
    assert await _fire(report_id, user_id, org_id) is should_run


@pytest.mark.e2e
def test_cancel_waits_for_user_drops_only_that_users_jobs():
    """Deprovisioning clears the user's pending waits, scoped to the org when given."""
    def _job(jid, uid, oid):
        return type("J", (), {"id": jid, "kwargs": {"user_id": uid, "organization_id": oid}})()

    jobs = [
        _job("wait:r1:a", "u1", "o1"),
        _job("wait:r2:b", "u1", "o2"),
        _job("wait:r3:c", "u2", "o1"),
        _job("sp:other", "u1", "o1"),  # not a wait job
    ]
    removed = []
    with patch.object(ws.scheduler, "get_jobs", lambda: jobs), \
         patch.object(ws.scheduler, "remove_job", lambda job_id: removed.append(job_id)):
        assert ws.wait_service.cancel_waits_for_user("u1", "o1") == ["wait:r1:a"]
        assert ws.wait_service.cancel_waits_for_user("u1") == ["wait:r1:a", "wait:r2:b"]
    assert "wait:r3:c" not in removed and "sp:other" not in removed
