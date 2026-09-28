"""MemoryService against a real DB (sqlite by default, postgres in CI).

Contracts: memory takes facts and refuses rules (on every write), dedupe
strengthens instead of duplicating (normalization variants, shared aliases),
a date merges into the existing fact, an edit changes the row in place,
forget blanks content, expiry is computed on read with a clock we
pass in, the cap evicts the weakest non-user entry and never a user-authored
one, and concurrent writers never lose an entry.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from app.dependencies import async_session_maker
from app.services import memory_rules as R
from app.services.memory_rules import MemoryValidationError
from app.services.memory_service import memory_service

NOW = datetime(2026, 10, 6, 9, 0)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def owner(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    who = whoami(token)
    return who["id"], who["organizations"][0]["id"]


async def _create(org, uid, text, tags=("work",), source="agent", **kw):
    async with async_session_maker() as db:
        r = await memory_service.create(
            db, organization_id=org, user_id=uid, text=text,
            tags=list(tags), source=source, now=kw.pop("now", NOW), **kw,
        )
        return r.entry.id, r.entry.handle, r.deduped


async def _active(org, uid, now=NOW, **kw):
    async with async_session_maker() as db:
        return await memory_service.active_entries(db, org, uid, now=now, **kw)


@pytest.mark.parametrize("variant", [
    "leads the q3 churn project", "  Leads the Q3 CHURN project!! ", "Leads the Q3 churn, project.",
])
def test_normalization_variants_strengthen_existing_entry(owner, variant):
    uid, org = owner
    first_id, h1, d1 = _run(_create(org, uid, "Leads the Q3 churn project."))
    same_id, h2, d2 = _run(_create(org, uid, variant, tags=["Q3 Churn"]))
    assert (d1, d2) == (False, True)
    assert same_id == first_id and h1 == h2
    [entry] = _run(_active(org, uid))
    assert entry.seen_count == 2
    assert set(entry.tags) == {"work", "q3-churn"}


def test_date_given_later_merges_into_the_same_fact(owner):
    uid, org = owner
    first, _, _ = _run(_create(org, uid, "Presents to the CFO", tags=["cfo"]))
    same, _, deduped = _run(_create(org, uid, "Presents to the CFO", tags=["cfo"], event_start="2026-10-20"))
    assert deduped and same == first
    [e] = _run(_active(org, uid))
    assert e.event_start == datetime(2026, 10, 20)


@pytest.mark.parametrize("source", ["agent", "user"])
@pytest.mark.parametrize("text", ["Prefers the number first", "Amounts in €M, one decimal", "Always exclude test accounts"])
def test_rules_are_refused_on_every_write(owner, source, text):
    uid, org = owner
    with pytest.raises(MemoryValidationError) as ei:
        _run(_create(org, uid, text, source=source))
    assert ei.value.code == "memory.looks_like_rule"
    assert _run(_active(org, uid)) == []


def test_shared_alias_merges(owner):
    uid, org = owner
    first, _, _ = _run(_create(org, uid, "\"my region\" = EMEA", tags=["emea"],
                               aliases=["my region"]))
    second, _, deduped = _run(_create(org, uid, "'my patch' = EMEA", tags=["emea"],
                                      aliases=["My Region", "my patch"]))
    assert deduped and second == first
    [e] = _run(_active(org, uid))
    assert {a.lower() for a in e.aliases} == {"my region", "my patch"}


def test_update_edits_in_place_and_keeps_the_handle(owner):
    uid, org = owner

    async def go():
        async with async_session_maker() as db:
            r = await memory_service.create(db, organization_id=org, user_id=uid, text="Leads the Q3 churn project", tags=["churn"], source="agent", now=NOW)
            handle, eid = r.entry.handle, str(r.entry.id)
            await memory_service.update(db, r.entry, changes={"text": "Leads the Q4 churn project", "event_start": "2026-12-01"}, source="user")
            resolved = await memory_service.resolve_handle(db, org, uid, handle)
            all_rows = await memory_service.list_entries(db, org, uid, statuses=("active", "forgotten"))
            return handle, eid, resolved, all_rows

    handle, eid, resolved, rows = _run(go())
    assert [str(r.id) for r in rows] == [eid]  # no second row
    assert resolved.handle == handle and resolved.text == "Leads the Q4 churn project"
    assert resolved.event_start == datetime(2026, 12, 1)
    assert resolved.source == "user"  # a user edit confirms the fact


def test_update_can_clear_a_date(owner):
    uid, org = owner

    async def go():
        async with async_session_maker() as db:
            r = await memory_service.create(db, organization_id=org, user_id=uid, text="Board meeting", tags=["board"], source="user", event_start="2026-10-09", now=NOW)
            return (await memory_service.update(db, r.entry, changes={"event_start": None}, source="user")).event_start

    assert _run(go()) is None


def test_forget_blanks_content_and_forget_all(owner):
    uid, org = owner

    async def go():
        async with async_session_maker() as db:
            a = (await memory_service.create(db, organization_id=org, user_id=uid, text="Owns the cohort reporting", tags=["charts"], source="user", now=NOW)).entry
            await memory_service.update(db, a, changes={"text": "Owns the cohort and retention reporting"}, source="user")
            n = await memory_service.forget_all(db, org, uid)
            rows = await memory_service.list_entries(db, org, uid, statuses=("active", "forgotten"))
            return n, rows

    n, rows = _run(go())
    assert n == 1
    assert rows and all(r.status == "forgotten" and r.text == "" and not r.tags and r.evidence is None for r in rows)


def test_undated_agent_fact_goes_stale_and_returns_when_seen_again(owner):
    uid, org = owner
    _run(_create(org, uid, "Investigating Q3 churn", tags=["churn"], now=NOW))
    assert len(_run(_active(org, uid, now=NOW + timedelta(days=89)))) == 1
    later = NOW + timedelta(days=91)
    assert _run(_active(org, uid, now=later)) == []
    # Seen again (the agent re-saves it) → strengthened and visible again.
    _, _, deduped = _run(_create(org, uid, "investigating q3 churn", tags=["churn"], now=later))
    assert deduped
    assert len(_run(_active(org, uid, now=later))) == 1


def test_dated_facts_hidden_after_their_date_but_kept_in_db(owner):
    uid, org = owner
    _run(_create(org, uid, "Board meeting", tags=["board"], event_start="2026-10-09"))
    assert len(_run(_active(org, uid, now=datetime(2026, 10, 10, 12)))) == 1
    assert _run(_active(org, uid, now=datetime(2026, 10, 11, 1))) == []
    assert len(_run(_active(org, uid, now=datetime(2026, 10, 11, 1), include_expired=True))) == 1


def test_cap_evicts_lowest_ranked_non_user_entry_and_never_user_entries(owner, monkeypatch):
    uid, org = owner
    monkeypatch.setattr(R, "MAX_ACTIVE_ENTRIES", 6)

    async def go():
        async with async_session_maker() as db:
            for i in range(3):
                await memory_service.create(db, organization_id=org, user_id=uid, text=f"User fact {i}", source="user", now=NOW)
            weak = (await memory_service.create(db, organization_id=org, user_id=uid, text="Agent weak", tags=["x"], source="agent",
                                                now=NOW - timedelta(days=9))).entry.handle
            for i in range(2):
                r = await memory_service.create(db, organization_id=org, user_id=uid, text=f"Agent strong {i}", tags=["x"], source="agent", now=NOW)
                await memory_service.touch(db, [r.entry], now=NOW)
            # 7th write: over the cap by one.
            r = await memory_service.create(db, organization_id=org, user_id=uid, text="Agent newest", tags=["x"], source="agent", now=NOW)
            live = await memory_service.active_entries(db, org, uid, now=NOW)
            return weak, r.evicted, live

    weak, evicted, live = _run(go())
    assert evicted == [weak]
    assert len(live) == 6
    assert sum(1 for e in live if e.source == "user") == 3


def test_cap_full_of_user_entries_refuses_instead_of_evicting(owner, monkeypatch):
    uid, org = owner
    monkeypatch.setattr(R, "MAX_ACTIVE_ENTRIES", 2)
    _run(_create(org, uid, "Mine 1", source="user"))
    _run(_create(org, uid, "Mine 2", source="user"))
    with pytest.raises(MemoryValidationError) as ei:
        _run(_create(org, uid, "Agent new", tags=["x"], source="agent"))
    assert ei.value.code == "memory.full"
    assert all(e.source == "user" for e in _run(_active(org, uid)))


def test_concurrent_writers_never_lose_entries(owner):
    uid, org = owner

    async def go():
        await asyncio.gather(*[
            _create(org, uid, f"Parallel fact {i}", tags=["p"]) for i in range(6)
        ])
        return await _active(org, uid)

    live = _run(go())
    assert len(live) == 6
    assert len({e.handle for e in live}) == 6


def test_search_scores_aliases_and_excludes_given_ids(owner):
    uid, org = owner
    vid, _, _ = _run(_create(org, uid, "\"my region\" = EMEA", tags=["emea"],
                             aliases=["my region"]))
    _run(_create(org, uid, "Investigating churn", tags=["churn"]))

    async def go(exclude=()):
        async with async_session_maker() as db:
            return await memory_service.search(db, org, uid, query="numbers for my region", exclude_ids=exclude, now=NOW)

    assert [e.id for e in _run(go())] == [vid]
    assert _run(go(exclude=[vid])) == []
