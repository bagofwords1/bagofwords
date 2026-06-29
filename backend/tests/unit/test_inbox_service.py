"""Unit tests for InboxService.list_for_user.

These cover the two failure modes behind the "red unread badge over an empty
inbox" bug:

  1. Ordering — the inbox reads newest-first (created_at DESC). A stale but
     higher-severity row must not jump above a fresher one.
  2. Resilience — the unread badge is fed by count_unread (a pure func.count
     that can't fail), while the list loads/sorts/serializes rows. If listing
     500s on a single malformed row the frontend swallows the error and shows
     "you're all caught up" under a non-zero badge. list_for_user must therefore
     never raise: a poison row degrades to a placeholder and the list stays
     consistent with the count.
"""

from datetime import datetime, timedelta

import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.notification import Notification
import app.services.inbox_service as inbox_mod
from app.services.inbox_service import inbox_service


async def _seed_user(db):
    org = Organization(name="Org")
    db.add(org)
    await db.flush()
    user = User(
        name="U",
        email="inbox-test@example.com",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        is_verified=True,
    )
    db.add(user)
    await db.flush()
    return org, user


def _notif(org, user, *, title, severity="info", created_at=None, **kw):
    return Notification(
        organization_id=str(org.id),
        user_id=str(user.id),
        source=kw.pop("source", "schedule"),
        type=kw.pop("type", "scheduled_run"),
        severity=severity,
        title=title,
        created_at=created_at,
        **kw,
    )


@pytest.mark.asyncio
async def test_list_orders_newest_first_regardless_of_severity():
    async with async_session_maker() as db:
        org, user = await _seed_user(db)
        now = datetime.utcnow()
        # Old but high-severity warning, plus a fresh info row.
        db.add(_notif(org, user, title="old-warning", severity="warning",
                      created_at=now - timedelta(hours=21)))
        db.add(_notif(org, user, title="fresh-info", severity="info",
                      created_at=now))
        await db.commit()

        res = await inbox_service.list_for_user(db, user)
        titles = [i["title"] for i in res["items"]]
        # Newest-first: the fresh info row wins over the stale warning.
        assert titles == ["fresh-info", "old-warning"], titles


@pytest.mark.asyncio
async def test_list_survives_a_poison_row_and_matches_count(monkeypatch):
    async with async_session_maker() as db:
        org, user = await _seed_user(db)
        now = datetime.utcnow()
        db.add(_notif(org, user, title="good", created_at=now))
        db.add(_notif(org, user, title="poison", created_at=now - timedelta(minutes=1)))
        await db.commit()

        # Make serialization explode for one row — mimics any per-row failure.
        orig = inbox_mod.to_dict

        def boom(n):
            if getattr(n, "title", "") == "poison":
                raise ValueError("simulated bad row")
            return orig(n)

        monkeypatch.setattr(inbox_mod, "to_dict", boom)

        # Must not raise, and must still return both rows (poison as placeholder).
        res = await inbox_service.list_for_user(db, user)
        assert len(res["items"]) == 2, res["items"]

        # The list's unread count stays consistent with the badge's count.
        count = await inbox_service.count_unread(db, user)
        assert res["unread"] == count["unread"] == 2


@pytest.mark.asyncio
async def test_list_handles_null_created_at():
    async with async_session_maker() as db:
        org, user = await _seed_user(db)
        db.add(_notif(org, user, title="no-timestamp", created_at=None))
        db.add(_notif(org, user, title="dated", created_at=datetime.utcnow()))
        await db.commit()

        res = await inbox_service.list_for_user(db, user)
        assert len(res["items"]) == 2
        assert res["unread"] == 2
