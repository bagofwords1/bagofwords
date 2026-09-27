"""MemoryService against a real DB (sqlite by default, postgres in CI).

Contracts: dedupe strengthens instead of duplicating (normalization variants,
vocabulary aliases), a different section is a different fact, supersede keeps
history, forget blanks content, expiry is computed on read with a clock we
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


async def _create(org, uid, text, section="style", tags=("style",), source="agent", **kw):
    async with async_session_maker() as db:
        r = await memory_service.create(
            db, organization_id=org, user_id=uid, text=text, section=section,
            tags=list(tags), source=source, now=kw.pop("now", NOW), **kw,
        )
        return r.entry.id, r.entry.handle, r.deduped


async def _active(org, uid, now=NOW, **kw):
    async with async_session_maker() as db:
        return await memory_service.active_entries(db, org, uid, now=now, **kw)


@pytest.mark.parametrize("variant", [
    "prefers the number first", "  Prefers the NUMBER first!! ", "Prefers the number, first.",
])
def test_normalization_variants_strengthen_existing_entry(owner, variant):
    uid, org = owner
    first_id, h1, d1 = _run(_create(org, uid, "Prefers the number first."))
    same_id, h2, d2 = _run(_create(org, uid, variant, tags=["Output Style"]))
    assert (d1, d2) == (False, True)
    assert same_id == first_id and h1 == h2
    [entry] = _run(_active(org, uid))
    assert entry.seen_count == 2
    assert set(entry.tags) == {"style", "output-style"}


def test_same_text_in_different_section_is_a_new_entry(owner):
    uid, org = owner
    _run(_create(org, uid, "Presents to the CFO monthly", section="role", tags=["cfo"]))
    _, _, deduped = _run(_create(org, uid, "Presents to the CFO monthly", section="events", tags=["cfo"],
                                 event_start="2026-10-20"))
    assert deduped is False
    assert len(_run(_active(org, uid))) == 2


def test_vocabulary_alias_match_merges(owner):
    uid, org = owner
    first, _, _ = _run(_create(org, uid, "\"my region\" = EMEA", section="vocabulary", tags=["emea"],
                               aliases=["my region"]))
    second, _, deduped = _run(_create(org, uid, "'my patch' = EMEA", section="vocabulary", tags=["emea"],
                                      aliases=["My Region", "my patch"]))
    assert deduped and second == first
    [e] = _run(_active(org, uid))
    assert {a.lower() for a in e.aliases} == {"my region", "my patch"}


def test_update_supersedes_and_old_handle_follows_to_new_version(owner):
    uid, org = owner

    async def go():
        async with async_session_maker() as db:
            r = await memory_service.create(db, organization_id=org, user_id=uid, text="Amounts in $K",
                                            section="style", tags=["currency"], source="agent", now=NOW)
            new = await memory_service.update(db, r.entry, changes={"text": "Amounts in €M"}, source="agent")
            resolved = await memory_service.resolve_handle(db, org, uid, r.entry.handle)
            all_rows = await memory_service.list_entries(db, org, uid, statuses=("active", "superseded"))
            return r.entry.handle, new.handle, resolved.handle, [(x.handle, x.status) for x in all_rows]

    old, new, resolved, rows = _run(go())
    assert new != old and resolved == new
    assert (old, "superseded") in rows and (new, "active") in rows


def test_forget_blanks_content_and_forget_all_covers_old_versions(owner):
    uid, org = owner

    async def go():
        async with async_session_maker() as db:
            a = (await memory_service.create(db, organization_id=org, user_id=uid, text="Likes cohort charts",
                                             section="preferences", tags=["charts"], source="user", now=NOW)).entry
            await memory_service.update(db, a, changes={"text": "Likes cohort tables"}, source="user")
            n = await memory_service.forget_all(db, org, uid)
            rows = await memory_service.list_entries(db, org, uid, statuses=("active", "superseded", "forgotten"))
            return n, rows

    n, rows = _run(go())
    assert n == 1
    assert rows and all(r.status == "forgotten" and r.text == "" and not r.tags and r.evidence is None for r in rows)


def test_focus_expires_after_30_days_and_returns_when_seen_again(owner):
    uid, org = owner
    _run(_create(org, uid, "Investigating Q3 churn", section="focus", tags=["churn"], now=NOW))
    assert len(_run(_active(org, uid, now=NOW + timedelta(days=29)))) == 1
    later = NOW + timedelta(days=31)
    assert _run(_active(org, uid, now=later)) == []
    # Seen again (the agent re-saves it) → strengthened and visible again.
    _, _, deduped = _run(_create(org, uid, "investigating q3 churn", section="focus", tags=["churn"], now=later))
    assert deduped
    assert len(_run(_active(org, uid, now=later))) == 1


def test_events_hidden_after_end_but_kept_in_db(owner):
    uid, org = owner
    _run(_create(org, uid, "Board meeting", section="events", tags=["board"], event_start="2026-10-09"))
    assert len(_run(_active(org, uid, now=datetime(2026, 10, 10, 12)))) == 1
    assert _run(_active(org, uid, now=datetime(2026, 10, 11, 1))) == []
    assert len(_run(_active(org, uid, now=datetime(2026, 10, 11, 1), include_expired=True))) == 1


def test_cap_evicts_lowest_ranked_non_user_entry_and_never_user_entries(owner, monkeypatch):
    uid, org = owner
    monkeypatch.setattr(R, "MAX_ACTIVE_ENTRIES", 6)

    async def go():
        async with async_session_maker() as db:
            for i in range(3):
                await memory_service.create(db, organization_id=org, user_id=uid, text=f"User pref {i}",
                                            section="preferences", source="user", now=NOW)
            weak = (await memory_service.create(db, organization_id=org, user_id=uid, text="Agent weak",
                                                section="preferences", tags=["x"], source="agent",
                                                now=NOW - timedelta(days=9))).entry.handle
            for i in range(2):
                r = await memory_service.create(db, organization_id=org, user_id=uid, text=f"Agent strong {i}",
                                                section="preferences", tags=["x"], source="agent", now=NOW)
                await memory_service.touch(db, [r.entry], now=NOW)
            # 7th write: over the cap by one.
            r = await memory_service.create(db, organization_id=org, user_id=uid, text="Agent newest",
                                            section="preferences", tags=["x"], source="agent", now=NOW)
            live = await memory_service.active_entries(db, org, uid, now=NOW)
            return weak, r.evicted, live

    weak, evicted, live = _run(go())
    assert evicted == [weak]
    assert len(live) == 6
    assert sum(1 for e in live if e.source == "user") == 3


def test_cap_full_of_user_entries_refuses_instead_of_evicting(owner, monkeypatch):
    uid, org = owner
    monkeypatch.setattr(R, "MAX_ACTIVE_ENTRIES", 2)
    _run(_create(org, uid, "Mine 1", section="preferences", source="user"))
    _run(_create(org, uid, "Mine 2", section="preferences", source="user"))
    with pytest.raises(MemoryValidationError) as ei:
        _run(_create(org, uid, "Agent new", section="preferences", tags=["x"], source="agent"))
    assert ei.value.code == "memory.full"
    assert all(e.source == "user" for e in _run(_active(org, uid)))


def test_concurrent_writers_never_lose_entries(owner):
    uid, org = owner

    async def go():
        await asyncio.gather(*[
            _create(org, uid, f"Parallel fact {i}", section="preferences", tags=["p"]) for i in range(6)
        ])
        return await _active(org, uid)

    live = _run(go())
    assert len(live) == 6
    assert len({e.handle for e in live}) == 6


def test_search_scores_aliases_and_excludes_given_ids(owner):
    uid, org = owner
    vid, _, _ = _run(_create(org, uid, "\"my region\" = EMEA", section="vocabulary", tags=["emea"],
                             aliases=["my region"]))
    _run(_create(org, uid, "Investigating churn", section="focus", tags=["churn"]))

    async def go(exclude=()):
        async with async_session_maker() as db:
            return await memory_service.search(db, org, uid, query="numbers for my region", exclude_ids=exclude, now=NOW)

    assert [e.id for e in _run(go())] == [vid]
    assert _run(go(exclude=[vid])) == []
