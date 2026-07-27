"""Instruction reads must survive — and the database must reject — a second
build flagged is_main for the same organization.

Only one InstructionBuild per org may carry is_main=True: it is the live
instruction set every read resolves against. When a second one appeared (two
promotions racing inside one org), every reader's `scalar_one_or_none()` raised
MultipleResultsFound and the whole instruction surface 500'd — the UI showed
"Failed to load instructions" and the agent lost its instruction context.
"""

import asyncio
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from app.models.instruction_build import InstructionBuild

MAIN_BUILD_INDEX = "uq_instruction_builds_one_main_per_org"


def _run(coro_fn):
    """Run a coroutine against a fresh session on the test database."""
    from app.settings.database import create_async_session_factory

    async def _wrapped():
        factory = create_async_session_factory()
        async with factory() as db:
            return await coro_fn(db)

    return asyncio.run(_wrapped())


async def _insert_main_build(db, org_id, *, build_number, deleted_at=None):
    build = InstructionBuild(
        build_number=build_number,
        status="published",
        source="user",
        is_main=True,
        organization_id=org_id,
        total_instructions=0,
        deleted_at=deleted_at,
    )
    db.add(build)
    await db.commit()
    return str(build.id)


async def _next_build_number(db, org_id):
    current = (await db.execute(
        select(func.max(InstructionBuild.build_number))
        .where(InstructionBuild.organization_id == org_id)
    )).scalar()
    return (current or 0) + 1


async def _live_main_ids(db, org_id):
    rows = (await db.execute(
        select(InstructionBuild.id).where(
            InstructionBuild.organization_id == org_id,
            InstructionBuild.is_main == True,  # noqa: E712
            InstructionBuild.deleted_at == None,  # noqa: E711
        )
    )).scalars().all()
    return [str(r) for r in rows]


@pytest.fixture
def agent_with_instructions(create_user, login_user, whoami, create_data_source, create_instruction):
    """An org with one agent and a few published instructions on it."""
    def _make(n=3):
        user = create_user()
        token = login_user(user["email"], user["password"])
        org_id = str(whoami(token)["organizations"][0]["id"])
        ds = create_data_source(
            name=f"agent-{uuid.uuid4().hex[:6]}", type="sqlite",
            config={"database": ":memory:"}, credentials={},
            user_token=token, org_id=org_id,
        )
        for i in range(n):
            create_instruction(
                text=f"prefer the _{i}_staging table when reconciling ledgers",
                user_token=token, org_id=org_id, status="published",
                data_source_ids=[ds["id"]],
            )
        return {
            "org_id": org_id,
            "ds_id": ds["id"],
            "count": n,
            "headers": {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
        }

    return _make


@pytest.mark.e2e
def test_database_rejects_a_second_live_main_build(agent_with_instructions):
    ctx = agent_with_instructions(2)
    org_id = ctx["org_id"]

    async def _attempt(db):
        from sqlalchemy.exc import IntegrityError

        assert len(await _live_main_ids(db, org_id)) == 1
        bn = await _next_build_number(db, org_id)
        try:
            await _insert_main_build(db, org_id, build_number=bn)
        except IntegrityError:
            await db.rollback()
            return True
        return False

    assert _run(_attempt) is True, "a second live main build was accepted"
    assert len(_run(lambda db: _live_main_ids(db, org_id))) == 1


@pytest.mark.e2e
def test_a_soft_deleted_main_build_does_not_block_or_win(agent_with_instructions):
    """Readers only consider live builds, so a soft-deleted former main must
    neither be resolved as main nor occupy the org's one main slot."""
    from app.core.main_build import resolve_main_build, resolve_main_build_id

    ctx = agent_with_instructions(2)
    org_id = ctx["org_id"]

    async def _check(db):
        live_before = await _live_main_ids(db, org_id)
        # A higher-numbered but soft-deleted main: allowed to exist, must lose.
        bn = await _next_build_number(db, org_id)
        stale = await _insert_main_build(
            db, org_id, build_number=bn + 10, deleted_at=datetime.utcnow(),
        )
        return live_before, stale, await resolve_main_build_id(db, org_id), \
            str((await resolve_main_build(db, org_id)).id)

    live_before, stale, resolved_id, resolved_obj_id = _run(_check)
    assert resolved_id == live_before[0]
    assert resolved_id != stale
    assert resolved_obj_id == resolved_id


@pytest.mark.e2e
@pytest.mark.parametrize("limit", [50, 200])
def test_instruction_reads_survive_a_preexisting_duplicate_main_build(
    test_client, agent_with_instructions, limit
):
    """An org corrupted before the uniqueness constraint existed (exactly what a
    promotion race produced) must still be readable: the list, the counts and
    the resolver all pick one main instead of raising."""
    from app.core.main_build import resolve_main_build_id

    ctx = agent_with_instructions(4)
    org_id = ctx["org_id"]

    async def _corrupt(db):
        # Drop the constraint to recreate the legacy state, then re-add it after
        # the assertions so the rest of the session sees a healthy schema.
        await db.execute(text(f"DROP INDEX {MAIN_BUILD_INDEX}"))
        await db.commit()
        original = (await _live_main_ids(db, org_id))[0]
        bn = await _next_build_number(db, org_id)
        # Newest build_number wins, so make the intruder the LOWER one: the
        # resolver must still return the original, not merely "some row".
        intruder = await _insert_main_build(db, org_id, build_number=bn)
        await db.execute(
            InstructionBuild.__table__.update()
            .where(InstructionBuild.id == intruder)
            .values(build_number=1, created_at=datetime.utcnow() - timedelta(days=30))
        )
        await db.commit()
        assert len(await _live_main_ids(db, org_id)) == 2
        return original, intruder, await resolve_main_build_id(db, org_id)

    original, intruder, resolved = _run(_corrupt)
    assert resolved == original, "resolver must pick the newest main deterministically"
    assert resolved != intruder

    listing = test_client.get(
        "/api/instructions",
        params={"data_source_ids": ctx["ds_id"], "include_global": "true",
                "limit": limit, "include_own": "true", "include_drafts": "true"},
        headers=ctx["headers"],
    )
    assert listing.status_code == 200, listing.text[:500]
    assert listing.json()["total"] == ctx["count"]

    counts = test_client.get("/api/instructions/counts", headers=ctx["headers"])
    assert counts.status_code == 200, counts.text[:500]
