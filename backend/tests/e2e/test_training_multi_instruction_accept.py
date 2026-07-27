"""Reproduction + regression for the training-mode multi-instruction accept bug.

Symptom (reported): in a training session, when a single completion produces
multiple ``create_instruction`` calls, accepting one of them makes it impossible
to accept any of the others — the next accept errors.

Root cause: every ``create_instruction`` in a training session shares ONE draft
build (``runtime_ctx['training_build_id']``). The training UI's "Accept" button
promotes that shared build via ``POST /builds/{id}/publish`` with
``instruction_ids=[one]``. ``publish_build`` (a) removes every sibling from the
build (``_filter_build_contents``) and (b) promotes the build to main. So after
the first accept the siblings are gone AND the build is ``approved``/``is_main``,
which makes the second accept 400 ("Build is already published").

The fix routes per-instruction acceptance through the existing cherry-pick
"build-of-one" path (``resolve_suggestion`` / the ``/instructions/{id}/resolve``
endpoint) instead of publishing the shared build. Each accept promotes exactly
one instruction and drops only that instruction from the shared draft, leaving
the siblings pending and independently acceptable.

These tests exercise the SERVICE layer (no LLM) so they run deterministically in
a clean sandbox. ``_seed_training_draft`` mirrors what the ``create_instruction``
tool does: a shared draft build with several new (not-in-main) instructions
staged at ``version_status_override='published'`` and ``auto_finalize=False``.
"""
import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.membership import Membership
from app.services.build_service import BuildService
from app.services.instruction_service import InstructionService
from app.schemas.instruction_schema import InstructionCreate


def _run(coro):
    return asyncio.run(coro)


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


async def _seed_training_draft(n_instructions=3):
    """Seed an org + admin, a shared draft build, and N new instructions staged
    into it exactly the way the training-mode create_instruction tool does."""
    suffix = uuid.uuid4().hex[:8]
    build_service = BuildService()
    instruction_service = InstructionService()

    async with async_session_maker() as db:
        org = Organization(name=f"Train Org {suffix}")
        db.add(org)
        await db.flush()

        admin = User(
            name="Admin", email=f"admin-{suffix}@example.com",
            hashed_password="x", is_active=True, is_superuser=False, is_verified=True,
        )
        db.add(admin)
        await db.flush()
        db.add(Membership(user_id=admin.id, organization_id=org.id, role="admin"))
        await db.commit()
        await db.refresh(org)
        await db.refresh(admin)

        # One shared draft build for the whole training session (lazy-created on
        # the first create_instruction — see create_instruction.py:248).
        build = await build_service.get_or_create_draft_build(
            db=db, org_id=str(org.id), source="ai", user_id=str(admin.id),
        )

        instruction_ids = []
        for i in range(n_instructions):
            data = InstructionCreate(
                text=f"Training rule number {i}: always exclude cancelled orders.",
                title=f"Rule {i}",
                category="general",
                load_mode="always",
                data_source_ids=[],
                references=[],
                status="draft",
            )
            inst = await instruction_service.create_instruction(
                db=db, instruction_data=data, current_user=admin, organization=org,
                force_global=True, build=build, auto_finalize=False,
                version_status_override="published",
            )
            instruction_ids.append(str(inst.id))

        return str(org.id), str(admin.id), str(build.id), instruction_ids


async def _build_content_ids(db, build_id):
    bs = BuildService()
    contents = await bs.get_build_contents(db, build_id)
    return {str(c.instruction_id) for c in contents}


async def _is_live(db, org_id, instruction_id):
    """True iff the instruction is present in the org's current main build."""
    from sqlalchemy import select
    from app.models.instruction_build import InstructionBuild
    from app.models.build_content import BuildContent
    row = (await db.execute(
        select(BuildContent.instruction_id)
        .join(InstructionBuild, InstructionBuild.id == BuildContent.build_id)
        .where(
            InstructionBuild.organization_id == org_id,
            InstructionBuild.is_main.is_(True),
            InstructionBuild.deleted_at.is_(None),
            BuildContent.instruction_id == instruction_id,
        ).limit(1)
    )).first()
    return row is not None


# ---------------------------------------------------------------------------
# Loop A.1 — the BUG, reproduced against the current "publish shared build" path
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_publish_shared_build_breaks_sibling_accepts():
    """CURRENT (buggy) behavior: accepting one instruction by publishing the
    shared draft filtered to that id prunes the siblings and finalizes the build,
    so accepting a sibling afterward raises 400.

    This documents the root cause; the fixed frontend must NOT use this path for
    per-instruction accept.
    """
    from fastapi import HTTPException

    org_id, user_id, build_id, iids = await _seed_training_draft(3)
    build_service = BuildService()

    async with async_session_maker() as db:
        # All three staged in the one shared draft.
        assert await _build_content_ids(db, build_id) == set(iids)

    # Accept the FIRST instruction the way the UI does today.
    async with async_session_maker() as db:
        await build_service.publish_build(db, build_id, user_id, instruction_ids=[iids[0]])

    async with async_session_maker() as db:
        # Data loss: siblings were pruned out of the (now promoted) build.
        remaining = await _build_content_ids(db, build_id)
        assert iids[1] not in remaining and iids[2] not in remaining, (
            f"siblings should have been pruned by publish; got {remaining}"
        )

    # Accepting a sibling now errors — the build is already published.
    with pytest.raises(HTTPException) as exc:
        async with async_session_maker() as db:
            await build_service.publish_build(db, build_id, user_id, instruction_ids=[iids[1]])
    assert exc.value.status_code == 400
    assert "already published" in str(exc.value.detail).lower()


# ---------------------------------------------------------------------------
# Loop A.2 — the FIX: per-instruction resolve promotes each independently
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_resolve_accepts_each_instruction_independently():
    """FIXED behavior: accepting each new instruction through resolve_suggestion
    (build-of-one) promotes it live and leaves the siblings pending and
    acceptable. All three end up live; none is lost."""
    org_id, user_id, build_id, iids = await _seed_training_draft(3)
    instruction_service = InstructionService()

    for idx, iid in enumerate(iids):
        async with async_session_maker() as db:
            org = await db.get(Organization, org_id)
            user = await db.get(User, user_id)
            resolved = await instruction_service.accept_staged_instruction(
                db, iid, build_id=build_id, organization=org, current_user=user,
            )
            assert resolved is not None, f"accept returned None for {iid}"

        # After each accept: this instruction is live; not-yet-accepted siblings
        # are still staged in the shared draft (independently acceptable).
        async with async_session_maker() as db:
            assert await _is_live(db, org_id, iid), f"instruction {idx} should be live after accept"
            remaining = await _build_content_ids(db, build_id)
            for later in iids[idx + 1:]:
                assert later in remaining, (
                    f"sibling {later} must stay in the draft after accepting {iid}; "
                    f"draft now has {remaining}"
                )

    # Every instruction is live in main.
    async with async_session_maker() as db:
        for idx, iid in enumerate(iids):
            assert await _is_live(db, org_id, iid), f"instruction {idx} missing from main at end"


# ---------------------------------------------------------------------------
# Loop B — full HTTP stack (routing + permission gate + endpoint)
# ---------------------------------------------------------------------------

async def _seed_into_org(org_id, user_email, n_instructions=3):
    """Stage N new instructions into a shared draft build for an EXISTING
    (fixture-bootstrapped) org + admin, mirroring the training tool."""
    build_service = BuildService()
    instruction_service = InstructionService()
    async with async_session_maker() as db:
        org = await db.get(Organization, org_id)
        admin = (await db.execute(select(User).where(User.email == user_email))).scalar_one()
        build = await build_service.get_or_create_draft_build(
            db=db, org_id=str(org.id), source="ai", user_id=str(admin.id),
        )
        iids = []
        for i in range(n_instructions):
            data = InstructionCreate(
                text=f"HTTP training rule {i}: treat amount as cents.",
                title=f"HTTP Rule {i}", category="general", load_mode="always",
                data_source_ids=[], references=[], status="draft",
            )
            inst = await instruction_service.create_instruction(
                db=db, instruction_data=data, current_user=admin, organization=org,
                force_global=True, build=build, auto_finalize=False,
                version_status_override="published",
            )
            iids.append(str(inst.id))
        return str(build.id), iids


@pytest.mark.e2e
def test_accept_staged_endpoint_accepts_all_siblings(
    create_user, login_user, whoami, test_client
):
    """Through the real HTTP endpoint: every sibling in a shared training draft
    can be accepted; each returns 200 and lands live. This is the end-to-end
    proof of the reported symptom being fixed."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    build_id, iids = _run(_seed_into_org(org_id, user["email"], 3))

    for idx, iid in enumerate(iids):
        resp = test_client.post(
            f"/api/instructions/{iid}/accept-staged",
            json={"build_id": build_id}, headers=_hdr(token, org_id),
        )
        # Every accept succeeds — the second/third no longer 400.
        assert resp.status_code == 200, f"accept {idx} failed: {resp.status_code} {resp.text}"

    # All three are live in main.
    async def _all_live():
        async with async_session_maker() as db:
            return [await _is_live(db, org_id, iid) for iid in iids]
    assert all(_run(_all_live())), "every accepted instruction should be live in main"


# ---------------------------------------------------------------------------
# Loop A.3 — the report-summary pill must retire once every staged instruction
# has been individually accepted (empty draft husk regression)
# ---------------------------------------------------------------------------

async def _seed_training_report(n_instructions=3):
    """Seed an org + admin + a training report whose completion has one
    ``create_instruction`` tool execution per staged instruction, mirroring what
    the training agent records. ``get_report_summary`` derives the pending build
    from these tool executions' ``result_json.build_id``."""
    from app.models.report import Report
    from app.models.completion import Completion
    from app.models.completion_block import CompletionBlock
    from app.models.tool_execution import ToolExecution
    from app.models.agent_execution import AgentExecution

    suffix = uuid.uuid4().hex[:8]
    build_service = BuildService()
    instruction_service = InstructionService()

    async with async_session_maker() as db:
        org = Organization(name=f"Summary Org {suffix}")
        db.add(org)
        await db.flush()

        admin = User(
            name="Admin", email=f"summary-{suffix}@example.com",
            hashed_password="x", is_active=True, is_superuser=False, is_verified=True,
        )
        db.add(admin)
        await db.flush()
        db.add(Membership(user_id=admin.id, organization_id=org.id, role="admin"))
        await db.flush()

        report = Report(
            title="Training session", slug=f"train-{suffix}", status="active",
            mode="training", user_id=admin.id, organization_id=org.id,
        )
        db.add(report)
        await db.flush()

        completion = Completion(
            prompt={"content": "teach me"}, completion={}, status="success",
            role="ai_system", message_type="ai_completion", report_id=report.id,
            user_id=admin.id,
        )
        db.add(completion)
        await db.flush()

        agent_exec = AgentExecution(
            completion_id=completion.id, organization_id=org.id, user_id=admin.id,
            report_id=report.id, status="success",
        )
        db.add(agent_exec)
        await db.flush()

        build = await build_service.get_or_create_draft_build(
            db=db, org_id=str(org.id), source="ai", user_id=str(admin.id),
        )

        instruction_ids = []
        for i in range(n_instructions):
            data = InstructionCreate(
                text=f"Summary rule {i}: exclude test accounts.",
                title=f"Rule {i}", category="general", load_mode="always",
                data_source_ids=[], references=[], status="draft",
            )
            inst = await instruction_service.create_instruction(
                db=db, instruction_data=data, current_user=admin, organization=org,
                force_global=True, build=build, auto_finalize=False,
                version_status_override="published",
            )
            instruction_ids.append(str(inst.id))

            te = ToolExecution(
                agent_execution_id=agent_exec.id, tool_name="create_instruction",
                status="success", success=True,
                arguments_json={"text": data.text, "category": "general"},
                result_json={"success": True, "instruction_id": str(inst.id), "build_id": str(build.id)},
            )
            db.add(te)
            await db.flush()
            db.add(CompletionBlock(
                completion_id=completion.id, agent_execution_id=agent_exec.id,
                source_type="tool", tool_execution_id=te.id, block_index=i,
                title="create_instruction", status="complete",
            ))

        await db.commit()
        return str(org.id), str(admin.id), str(report.id), str(build.id), instruction_ids


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_summary_pending_build_clears_after_all_accepted():
    """Regression: the report-summary "publish" pill must disappear once every
    staged create_instruction has been individually accepted.

    accept_staged_instruction detaches each instruction from the shared draft but
    leaves the draft in `draft` status. get_report_summary used to key the pending
    build purely off build status, so the emptied husk kept surfacing as
    pending_training_build — nagging the user to publish changes already live."""
    from app.services.report_service import ReportService

    org_id, user_id, report_id, build_id, iids = await _seed_training_report(3)
    report_service = ReportService()
    instruction_service = InstructionService()

    # Before accepting anything: the pill is live with all three staged.
    async with async_session_maker() as db:
        summary = await report_service.get_report_summary(db, report_id)
    assert summary["pending_training_build"] is not None
    assert summary["pending_training_build"]["total_instructions"] == 3
    assert len(summary["instructions"]) == 3

    # Accept two of the three: the pill stays, now scoped to the remaining one.
    for iid in iids[:2]:
        async with async_session_maker() as db:
            org = await db.get(Organization, org_id)
            user = await db.get(User, user_id)
            await instruction_service.accept_staged_instruction(
                db, iid, build_id=build_id, organization=org, current_user=user,
            )
    async with async_session_maker() as db:
        summary = await report_service.get_report_summary(db, report_id)
    assert summary["pending_training_build"] is not None
    assert summary["pending_training_build"]["total_instructions"] == 1
    assert [i.instruction_id for i in summary["instructions"]] == [iids[2]]

    # Accept the last one: the draft is now an empty husk — no more pill.
    async with async_session_maker() as db:
        org = await db.get(Organization, org_id)
        user = await db.get(User, user_id)
        await instruction_service.accept_staged_instruction(
            db, iids[2], build_id=build_id, organization=org, current_user=user,
        )
    async with async_session_maker() as db:
        summary = await report_service.get_report_summary(db, report_id)
    assert summary["pending_training_build"] is None, (
        "pill must retire once every staged instruction has been accepted"
    )
    assert summary["instructions"] == []


@pytest.mark.e2e
def test_accept_staged_then_reject_sibling(
    create_user, login_user, whoami, test_client
):
    """Accept the first staged instruction, then reject the second: the reject
    still works (the draft was never finalized) and the third remains
    acceptable."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    build_id, iids = _run(_seed_into_org(org_id, user["email"], 3))

    # Accept #0
    assert test_client.post(
        f"/api/instructions/{iids[0]}/accept-staged",
        json={"build_id": build_id}, headers=_hdr(token, org_id),
    ).status_code == 200

    # Reject #1 (delete the never-published instruction)
    assert test_client.delete(
        f"/api/instructions/{iids[1]}", headers=_hdr(token, org_id),
    ).status_code in (200, 204)

    # Accept #2 still works
    assert test_client.post(
        f"/api/instructions/{iids[2]}/accept-staged",
        json={"build_id": build_id}, headers=_hdr(token, org_id),
    ).status_code == 200

    async def _states():
        async with async_session_maker() as db:
            return (
                await _is_live(db, org_id, iids[0]),
                await _is_live(db, org_id, iids[2]),
            )
    live0, live2 = _run(_states())
    assert live0 and live2, "accepted instructions #0 and #2 should be live"

    # The rejected instruction is gone.
    assert test_client.get(
        f"/api/instructions/{iids[1]}", headers=_hdr(token, org_id),
    ).status_code in (403, 404)
