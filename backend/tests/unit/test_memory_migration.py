"""Legacy Membership.memory → memory_entries migration.

Contracts: every non-empty line/bullet becomes one ``preferences`` entry with
source='migration' and no tags; running it again adds nothing; lines that
read like business definitions are migrated as-is and reported for review.
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
        "- Prefers USD\n- Concise answers, no emoji\n\n# misc\n* Active customers are those who paid in 90 days",
        "Likes cohort charts\nWants the SQL shown",
    )
    for (uid, o), doc in zip(users, docs):
        _seed(uid, o, doc)

    logged: list[str] = []
    monkeypatch.setattr(memory_migration.logger, "warning", lambda msg, *a, **k: logged.append(msg % a))
    first = _migrate()
    assert first.entries == 5 and first.memberships == 2
    e1 = _entries(*users[0])
    assert [e.text for e in e1] == ["Prefers USD", "Concise answers, no emoji",
                                    "Active customers are those who paid in 90 days"]
    assert all(e.section == "preferences" and e.source == "migration" and not e.tags for e in e1)
    assert [e.handle for e in e1] == ["m1", "m2", "m3"]
    # The definition-like line is reported for manual review; personal lines never are.
    assert [d["handle"] for d in first.definition_like] == ["m3"]
    assert any("business definition" in m and "Active customers" in m for m in logged)
    assert not any("Prefers USD" in m or "cohort" in m for m in logged)

    second = _migrate()
    assert second.entries == 0 and second.skipped_already_migrated == 2
    assert len(_entries(*users[0])) == 3 and len(_entries(*users[1])) == 2
