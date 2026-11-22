"""Records the purges a change to members' groups runs, and runs them.

A change to someone's groups changes the row filters compiled for them, so
their stored viewer results it outdates are purged once the change has
committed (app/services/viewer_result_freshness.purge_members_results). The
writers of group memberships (the admin API, SCIM, LDAP and OIDC sync, an
invite with groups) call it, after their commit, with the members whose
groups changed.

The purge stays real (tests/AGENTS.md: everything inside the app runs real):
each call is recorded and then handed to the application's purge, so a test
asserts the stored results it deleted or cleared (tests/fixtures
/member_results.py). The record adds what the stored results cannot show:
when the call came and with what. Each call holds each member's groups as
committed at that moment (read in a session of its own, so an uncommitted
change does not show) and the audit rows committed by then, counted by
(resource_id, action): a purge runs after the change it follows is audited.
It also records the time budget the writer gave the purge (None: the
default).
"""
from collections import Counter

import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker


@pytest.fixture
def member_purges(monkeypatch):
    import app.services.viewer_result_freshness as freshness
    from app.ee.audit.models import AuditLog
    from app.models.group_membership import GroupMembership

    calls: list[dict] = []
    purge = freshness.purge_members_results

    async def record_then_purge(db, *, organization_id, user_ids, budget_seconds=None):
        user_ids = list(user_ids)
        members = sorted({str(u) for u in user_ids if u})
        if members:
            groups: dict[str, set] = {u: set() for u in members}
            async with async_session_maker() as own:
                for user_id, group_id in (await own.execute(
                    select(GroupMembership.user_id, GroupMembership.group_id)
                    .where(GroupMembership.user_id.in_(members))
                )).all():
                    groups[str(user_id)].add(str(group_id))
                audits = Counter((await own.execute(
                    select(AuditLog.resource_id, AuditLog.action)
                    .where(AuditLog.organization_id == str(organization_id))
                )).all())
            calls.append({"organization_id": str(organization_id), "user_ids": members, "groups": groups,
                          "audits": audits, "budget_seconds": budget_seconds})
        return await purge(db, organization_id=organization_id, user_ids=user_ids,
                           budget_seconds=budget_seconds)

    monkeypatch.setattr(freshness, "purge_members_results", record_then_purge)
    return calls
