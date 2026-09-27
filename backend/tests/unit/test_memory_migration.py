"""Legacy Membership.memory → facts in memory, rules in Custom instructions.

Contracts: each non-empty line/bullet is sorted by the rule-vs-fact test —
facts become memory entries (source='migration', no tags), rules are appended
to the user's Custom instructions (Membership.note) while they fit; rules that
don't fit are logged and left in the read-only legacy column; running it again
changes nothing.
"""
import asyncio
import uuid

import pytest
from sqlalchemy import update

from app.dependencies import async_session_maker
from app.models.membership import Membership
from app.services import memory_migration
from app.services.memory_migration import migrate_legacy_memory
from app.services.memory_service import memory_service


def _seed(uid, org, text):
    # The legacy column is no longer writable through the API (read-only for
    # one release), so a direct write is the only way to produce this state.
    async def go():
        async with async_session_maker() as db:
            await db.execute(
                update(Membership).where(Membership.user_id == uid, Membership.organization_id == org).values(memory=text)
            )
            await db.commit()
    asyncio.run(go())


def _migrate():
    async def go():
        async with async_session_maker() as db:
            conn = await db.connection()
            report = await conn.run_sync(lambda sync_conn: migrate_legacy_memory(sync_conn))
            await db.commit()
            return report
    return asyncio.run(go())


def _entries(uid, org):
    async def go():
        async with async_session_maker() as db:
            return await memory_service.list_entries(db, org, uid)
    return asyncio.run(go())


def test_migration_splits_lines_and_is_idempotent(test_client, create_user, login_user, whoami, monkeypatch):
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    who = whoami(admin_token)
    org = who["organizations"][0]["id"]
    member_email = f"mig_{uuid.uuid4().hex[:8]}@test.com"
    r = test_client.post(
        f"/api/organizations/{org}/members",
        json={"organization_id": org, "email": member_email, "role": "member"},
        headers={"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org},
    )
    assert r.status_code == 200, r.json()
    create_user(email=member_email, password="test123")
    member_id = whoami(login_user(member_email, "test123"))["id"]

    users = [(who["id"], org), (member_id, org)]
    docs = (
        "- Prefers USD\n- Leads the Q3 churn project\n\n# misc\n* Presents to the CFO monthly\n- Always exclude test accounts",
        # rules that overflow the 500-char Custom instructions cap
        "\n".join(f"- Always show chart style number {i} with extra words to take space" for i in range(12)),
    )
    for (uid, o), doc in zip(users, docs):
        _seed(uid, o, doc)

    logged: list[str] = []
    monkeypatch.setattr(memory_migration.logger, "warning", lambda msg, *a, **k: logged.append(msg % a))
    first = _migrate()
    assert first.memberships == 2
    e1 = _entries(*users[0])
    assert [e.text for e in e1] == ["Leads the Q3 churn project", "Presents to the CFO monthly"]
    assert all(e.source == "migration" and not e.tags for e in e1)
    assert [e.handle for e in e1] == ["m1", "m2"]
    note1 = _note(*users[0])
    assert "- Prefers USD" in note1 and "- Always exclude test accounts" in note1
    assert "Leads the Q3 churn" not in note1

    # Second user: nothing is a fact, the rules fill the note to its cap and the rest are logged.
    assert _entries(*users[1]) == []
    note2 = _note(*users[1])
    assert len(note2) <= 500 and first.rules_not_moved
    assert len(logged) == len(first.rules_not_moved) and all("did not fit" in m for m in logged)

    second = _migrate()
    assert second.entries == 0 and second.rules_to_instructions == 0 and second.skipped_already_migrated == 2
    assert len(_entries(*users[0])) == 2 and _note(*users[0]) == note1 and _note(*users[1]) == note2


def _note(uid, org):
    from sqlalchemy import select

    async def go():
        async with async_session_maker() as db:
            return (await db.execute(select(Membership.note).where(
                Membership.user_id == uid, Membership.organization_id == org))).scalar_one()
    return asyncio.run(go())
