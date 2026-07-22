"""Regression: cross-agent authority for shared instructions in the automation plane.

A shared instruction is a single row loaded by every agent it is attached to.
Promoting a change to it makes that change live for ALL attached agents. Authority
is resolved per-agent, so the Self-Learning automation plane must NOT auto-promote
a suggestion touching a shared instruction unless EVERY affected agent's policy
independently consents (mode auto_approve / eval_auto).

Scenario (the reported design issue):
  instruction1 is attached to agent1 AND agent2. A manager controls only agent2
  and sets its policy to auto_approve. A suggestion build touching instruction1
  must not go live org-wide off agent2's policy alone — that would break agent1.

See docs/design/shared-instruction-editing.md.
"""
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.membership import Membership
from app.models.data_source import DataSource
from app.models.instruction_build import InstructionBuild
from app.models.build_content import BuildContent
from app.schemas.instruction_schema import InstructionCreate
from app.services.instruction_service import InstructionService
from app.services.build_service import BuildService
from app.services.agent_reliability_service import AgentReliabilityService

from sqlalchemy import select, and_


async def _seed_two_agents(ds1_settings, ds2_settings):
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Share Org {suffix}")
        db.add(org)
        await db.flush()

        admin = User(
            name="Admin", email=f"admin-{suffix}@example.com",
            hashed_password="x", is_active=True, is_superuser=False, is_verified=True,
        )
        db.add(admin)
        await db.flush()
        db.add(Membership(user_id=admin.id, organization_id=org.id, role="admin"))

        ds1 = DataSource(
            name=f"agent1-{suffix}", organization_id=org.id, is_active=True,
            owner_user_id=admin.id, automation_settings=ds1_settings,
        )
        ds2 = DataSource(
            name=f"agent2-{suffix}", organization_id=org.id, is_active=True,
            owner_user_id=admin.id, automation_settings=ds2_settings,
        )
        db.add(ds1)
        db.add(ds2)
        await db.flush()
        await db.commit()
        return str(org.id), str(admin.id), str(ds1.id), str(ds2.id)


async def _make_suggestion_build(org_id, admin_id, ds_ids):
    """Stage one instruction attached to ``ds_ids`` in a non-main draft build."""
    async with async_session_maker() as db:
        org = await db.get(Organization, org_id)
        admin = await db.get(User, admin_id)
        svc = InstructionService()
        schema = await svc.create_instruction(
            db,
            InstructionCreate(
                text="shared rule", status="published", category="general",
                data_source_ids=list(ds_ids),
            ),
            current_user=admin, organization=org,
            force_global=True, auto_finalize=False,  # stage only — do NOT promote
        )
        await db.commit()
        instr_id = str(schema.id)

        # Find the draft build that carries this instruction.
        row = (await db.execute(
            select(InstructionBuild)
            .join(BuildContent, BuildContent.build_id == InstructionBuild.id)
            .where(and_(
                BuildContent.instruction_id == instr_id,
                InstructionBuild.organization_id == str(org_id),
                InstructionBuild.is_main.is_(False),
                InstructionBuild.deleted_at.is_(None),
            ))
            .limit(1)
        )).scalar_one_or_none()
        assert row is not None, "expected a staged non-main build for the instruction"
        return str(row.id), instr_id


async def _build_is_main(build_id):
    async with async_session_maker() as db:
        b = await db.get(InstructionBuild, build_id)
        return bool(b.is_main)


async def _run_suggestion(org_id, build_id):
    async with async_session_maker() as db:
        org = await db.get(Organization, org_id)
        return await AgentReliabilityService().run_for_suggestion(db, org, build_id)


AUTO = {"mode": "auto_approve"}
OFF = {"mode": "off"}


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_shared_instruction_not_autopromoted_when_one_agent_dissents():
    """agent2=auto_approve, agent1=off. A build touching an instruction shared by
    both must stay pending — agent1 never consented."""
    org_id, admin_id, ds1, ds2 = await _seed_two_agents(ds1_settings=OFF, ds2_settings=AUTO)
    build_id, _ = await _make_suggestion_build(org_id, admin_id, [ds1, ds2])

    await _run_suggestion(org_id, build_id)

    assert not await _build_is_main(build_id), (
        "shared instruction must NOT auto-promote org-wide off one agent's policy"
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_scoped_instruction_autopromotes_for_consenting_agent():
    """Control: an instruction scoped ONLY to the auto_approve agent still
    auto-promotes — the guard must not over-block the single-agent case."""
    org_id, admin_id, _ds1, ds2 = await _seed_two_agents(ds1_settings=OFF, ds2_settings=AUTO)
    build_id, _ = await _make_suggestion_build(org_id, admin_id, [ds2])

    await _run_suggestion(org_id, build_id)

    assert await _build_is_main(build_id), (
        "a suggestion scoped entirely to a consenting agent should auto-promote"
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_shared_instruction_autopromotes_when_all_agents_consent():
    """Both agents auto_approve → the shared change may go live."""
    org_id, admin_id, ds1, ds2 = await _seed_two_agents(ds1_settings=AUTO, ds2_settings=AUTO)
    build_id, _ = await _make_suggestion_build(org_id, admin_id, [ds1, ds2])

    await _run_suggestion(org_id, build_id)

    assert await _build_is_main(build_id), (
        "when every affected agent consents, the shared change should promote"
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_global_instruction_not_autopromoted_off_one_agent():
    """A global instruction (no attached agents) affects every agent. One agent on
    auto_approve must not push a global change live for the whole org."""
    org_id, admin_id, _ds1, _ds2 = await _seed_two_agents(ds1_settings=OFF, ds2_settings=AUTO)
    build_id, _ = await _make_suggestion_build(org_id, admin_id, [])  # global

    await _run_suggestion(org_id, build_id)

    assert not await _build_is_main(build_id), (
        "a global instruction must not auto-promote off a single agent's policy"
    )
