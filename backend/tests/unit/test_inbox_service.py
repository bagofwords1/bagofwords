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

pytestmark = pytest.mark.db  # opens real sessions — see tests/unit/conftest.py



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

        res = await inbox_service.list_for_user(db, user, org)
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
        res = await inbox_service.list_for_user(db, user, org)
        assert len(res["items"]) == 2, res["items"]

        # The list's unread count stays consistent with the badge's count.
        count = await inbox_service.count_unread(db, user, org)
        assert res["unread"] == count["unread"] == 2


@pytest.mark.asyncio
async def test_list_handles_null_created_at():
    async with async_session_maker() as db:
        org, user = await _seed_user(db)
        db.add(_notif(org, user, title="no-timestamp", created_at=None))
        db.add(_notif(org, user, title="dated", created_at=datetime.utcnow()))
        await db.commit()

        res = await inbox_service.list_for_user(db, user, org)
        assert len(res["items"]) == 2
        assert res["unread"] == 2


@pytest.mark.asyncio
async def test_list_returns_the_actor_name():
    # The inbox shows who caused a notification (the sharer, the requester…).
    async with async_session_maker() as db:
        org, user = await _seed_user(db)
        actor = User(name="Dana Sharer", email="actor-inbox@example.com", hashed_password="x",
                     is_active=True, is_superuser=False, is_verified=True)
        db.add(actor)
        await db.flush()
        db.add(_notif(org, user, title="shared", created_at=datetime.utcnow(),
                      source="share", type="share_artifact", actor_user_id=str(actor.id)))
        db.add(_notif(org, user, title="system", created_at=datetime.utcnow() - timedelta(minutes=1)))
        await db.commit()

        res = await inbox_service.list_for_user(db, user, org)
        by_title = {i["title"]: i for i in res["items"]}
        assert by_title["shared"]["actor"] == {"id": str(actor.id), "name": "Dana Sharer"}
        assert by_title["system"]["actor"] is None


@pytest.mark.asyncio
async def test_resolved_rows_sort_below_open_ones():
    # A handled item stays visible (greyed) but must not crowd out open work.
    async with async_session_maker() as db:
        org, user = await _seed_user(db)
        now = datetime.utcnow()
        db.add(_notif(org, user, title="resolved-fresh", created_at=now,
                      resolved_at=now, resolution="resolved", read_at=now))
        db.add(_notif(org, user, title="open-old", created_at=now - timedelta(hours=5)))
        await db.commit()

        res = await inbox_service.list_for_user(db, user, org)
        assert [i["title"] for i in res["items"]] == ["open-old", "resolved-fresh"]
        assert res["items"][1]["resolved"] is True
        assert res["items"][1]["resolution"] == "resolved"


@pytest.mark.asyncio
async def test_handled_rows_drop_out_of_the_list_after_the_visible_window():
    from app.models.notification import RESOLVED_VISIBLE_DAYS
    async with async_session_maker() as db:
        org, user = await _seed_user(db)
        now = datetime.utcnow()
        old = now - timedelta(days=RESOLVED_VISIBLE_DAYS + 1)
        recent = now - timedelta(days=RESOLVED_VISIBLE_DAYS - 1)
        db.add(_notif(org, user, title="handled-long-ago", created_at=old, resolved_at=old, resolution="resolved", read_at=old))
        db.add(_notif(org, user, title="handled-recently", created_at=recent, resolved_at=recent, resolution="resolved", read_at=recent))
        db.add(_notif(org, user, title="open-old", created_at=old))
        await db.commit()

        titles = {i["title"] for i in (await inbox_service.list_for_user(db, user, org))["items"]}
        assert titles == {"handled-recently", "open-old"}
