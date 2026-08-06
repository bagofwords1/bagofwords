"""
Regression guard for completions cursor pagination (reports/{id} transcript).

Scrolling a conversation to the top pages through GET /api/reports/{id}/completions
with a `before` cursor. The cursor used to be the bare `created_at` of the oldest
row on the page with a strict `<` filter and no ORDER BY tiebreaker, so
completions sharing a created_at (bulk inserts, webhook bursts, fork imports)
were either skipped (silently missing history) or re-served forever (the client
deduped them to nothing and the scroll stalled with no signal).

The fixed cursor is "<ISO8601>~<completion id>" with (created_at, id) ordering,
which pages exactly once over ties. Bare ISO cursors from older clients must
keep working.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/test_pagination.db \
      .venv/bin/python -m pytest tests/e2e/test_completions_v2_pagination.py -v -s
"""
import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.report import Report
from app.models.completion import Completion
from app.services.completion_service import CompletionService


N_TURNS = 30  # 60 completions total


def _run(coro):
    return asyncio.run(coro)


async def _seed(collide: bool):
    suffix = uuid.uuid4().hex[:8]
    base = datetime(2026, 1, 1, 10, 0, 0)

    async with async_session_maker() as db:
        org = Organization(name=f"Pag Org {suffix}")
        db.add(org)
        await db.flush()

        user = User(
            name="Pag User",
            email=f"pag-{suffix}@example.com",
            hashed_password="x",
            is_active=True,
            is_superuser=False,
            is_verified=True,
        )
        db.add(user)
        await db.flush()

        report = Report(
            title=f"Pag Report {suffix}",
            slug=f"pag-report-{suffix}",
            status="draft",
            user_id=user.id,
            organization_id=org.id,
        )
        db.add(report)
        await db.flush()

        ids = []
        for i in range(N_TURNS):
            if collide:
                # groups of 4 completions share the exact same created_at,
                # deliberately straddling the limit=10 page boundaries
                t_user = t_sys = base + timedelta(minutes=(i // 2) * 5)
            else:
                t_user = base + timedelta(minutes=i * 5, microseconds=i * 137)
                t_sys = t_user + timedelta(seconds=2)

            u = Completion(
                prompt={"content": f"q{i}"}, completion=None, status="success",
                role="user", report_id=report.id, user_id=user.id, turn_index=i,
            )
            u.created_at = u.updated_at = t_user
            s = Completion(
                prompt=None, completion={"content": f"a{i}"}, status="success",
                role="system", report_id=report.id, turn_index=i,
            )
            s.created_at = s.updated_at = t_sys
            db.add_all([u, s])
            await db.flush()
            ids.extend([str(u.id), str(s.id)])

        await db.commit()
        return {"org": org, "user": user, "report_id": str(report.id), "ids": set(ids)}


async def _walk_pages(svc, seeded, limit=10, max_pages=20):
    seen, dups, pages = set(), 0, 0
    before = None
    async with async_session_maker() as db:
        while True:
            resp = await svc.get_completions_v2(
                db, seeded["report_id"], seeded["org"], seeded["user"],
                limit=limit, before=before,
            )
            pages += 1
            for c in resp.completions:
                if c.id in seen:
                    dups += 1
                seen.add(c.id)
            if not resp.has_more or pages >= max_pages:
                return seen, dups, pages, resp
            assert resp.next_before, "has_more without a next_before cursor"
            assert resp.next_before != before, "cursor did not advance"
            before = resp.next_before


@pytest.mark.e2e
def test_cursor_pages_exactly_once_over_identical_timestamps():
    async def scenario():
        svc = CompletionService()
        for collide in (False, True):
            seeded = await _seed(collide=collide)
            seen, dups, pages, _ = await _walk_pages(svc, seeded)
            label = "collide" if collide else "distinct"
            print(f"[pagination:{label}] pages={pages} unique={len(seen)} dups={dups}")
            assert dups == 0, f"{label}: {dups} completions served twice"
            assert seen == seeded["ids"], (
                f"{label}: {len(seeded['ids'] - seen)} completions never served "
                f"(silently lost history)")
            assert pages == (2 * N_TURNS) // 10, f"{label}: unexpected page count {pages}"

    _run(scenario())


@pytest.mark.e2e
def test_public_share_cursor_pages_exactly_once_over_identical_timestamps():
    """Same tie-safety guard for GET /api/c/{token} (public shared conversation),
    whose cursor is a completion id resolved server-side."""
    async def scenario():
        from app.services.report_service import ReportService
        svc = ReportService()
        for collide in (False, True):
            seeded = await _seed(collide=collide)
            token = f"tok-{uuid.uuid4().hex}"
            async with async_session_maker() as db:
                report = await db.get(Report, seeded["report_id"])
                report.conversation_share_token = token
                report.conversation_visibility = "public"
                await db.commit()

            seen, dups, pages = set(), 0, 0
            before = None
            async with async_session_maker() as db:
                while True:
                    resp = await svc.get_public_conversation(db, token, limit=10, before=before)
                    pages += 1
                    for c in resp["completions"]:
                        cid = str(c["id"] if isinstance(c, dict) else c.id)
                        if cid in seen:
                            dups += 1
                        seen.add(cid)
                    if not resp["has_more"] or pages >= 20:
                        break
                    assert resp["next_before"], "has_more without a next_before cursor"
                    before = str(resp["next_before"])

            label = "collide" if collide else "distinct"
            print(f"[public-pagination:{label}] pages={pages} unique={len(seen)} dups={dups}")
            assert dups == 0, f"{label}: {dups} completions served twice"
            assert seen == seeded["ids"], (
                f"{label}: {len(seeded['ids'] - seen)} completions never served "
                f"(silently lost history)")

    _run(scenario())


@pytest.mark.e2e
def test_cursor_format_and_legacy_iso_cursor_still_work():
    async def scenario():
        svc = CompletionService()
        seeded = await _seed(collide=False)

        async with async_session_maker() as db:
            first = await svc.get_completions_v2(
                db, seeded["report_id"], seeded["org"], seeded["user"], limit=10)
            assert first.has_more
            # compound "<iso>~<id>" cursor, pointing at the oldest row served
            ts_part, sep, id_part = first.next_before.partition("~")
            assert sep == "~" and id_part == first.completions[0].id
            datetime.fromisoformat(ts_part)  # parseable timestamp

            # an old client sending the bare ISO timestamp still pages backwards
            legacy = await svc.get_completions_v2(
                db, seeded["report_id"], seeded["org"], seeded["user"],
                limit=10, before=ts_part)
            assert legacy.completions, "legacy ISO cursor returned nothing"
            first_ids = {c.id for c in first.completions}
            assert all(c.id not in first_ids for c in legacy.completions)

            # unparseable cursor is ignored (serves the newest page) instead of 500ing
            junk = await svc.get_completions_v2(
                db, seeded["report_id"], seeded["org"], seeded["user"],
                limit=10, before="not-a-cursor")
            assert {c.id for c in junk.completions} == first_ids

    _run(scenario())
