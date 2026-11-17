"""Records the purges a change to members' groups runs.

A change to someone's groups changes the row filters compiled for them, so
their stored viewer results it outdates are purged once the change has
committed (app/services/viewer_result_freshness.purge_members_results). The
purge itself is covered in tests/e2e/rbac/test_viewer_result_freshness.py;
the writers of group memberships (the admin API, SCIM, LDAP and OIDC sync,
an invite with groups) only have to call it, after their commit, with the
members whose groups changed. This fixture stands in for it and records each
call with each member's groups as committed at that moment (read in a session
of its own, so an uncommitted change does not show), and the audit rows
committed by then, counted by (resource_id, action): a purge runs after the
change it follows is audited. It also records the time budget the writer
gave the purge (None: the default).
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

    async def record(db, *, organization_id, user_ids, budget_seconds=None):
        user_ids = sorted({str(u) for u in user_ids if u})
        if not user_ids:
            return  # as the purge: no member, nothing to purge
        groups: dict[str, set] = {u: set() for u in user_ids}
        async with async_session_maker() as own:
            for user_id, group_id in (await own.execute(
                select(GroupMembership.user_id, GroupMembership.group_id)
                .where(GroupMembership.user_id.in_(user_ids))
            )).all():
                groups[str(user_id)].add(str(group_id))
            audits = Counter((await own.execute(
                select(AuditLog.resource_id, AuditLog.action)
                .where(AuditLog.organization_id == str(organization_id))
            )).all())
        calls.append({"organization_id": str(organization_id), "user_ids": user_ids, "groups": groups,
                      "audits": audits, "budget_seconds": budget_seconds})

    monkeypatch.setattr(freshness, "purge_members_results", record)
    return calls
