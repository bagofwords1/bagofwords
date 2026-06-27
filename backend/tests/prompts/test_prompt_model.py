"""Prompt model: multi-agent association, access scoping, global scope,
inactive-agent handling, and parameters/mentions round-trip.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      .venv/bin/python -m pytest tests/prompts/test_prompt_model.py -v -s
"""
import uuid
import pytest
from fastapi import HTTPException

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.data_source import DataSource
from app.models.resource_grant import ResourceGrant
from app.models.membership import Membership
from app.models.role import Role
from app.models.role_assignment import RoleAssignment

from app.services.prompt_service import prompt_service
from app.schemas.prompt_schema import PromptCreate, PromptUpdate, PromptParameter


def _u(suffix, name):
    return User(name=f"{name} {suffix}", email=f"{name.lower()}-{suffix}@example.com",
                hashed_password="x", is_active=True, is_verified=True)


async def _grant(db, org_id, ds_id, user_id, perms):
    db.add(ResourceGrant(
        organization_id=org_id, resource_type="data_source", resource_id=ds_id,
        principal_type="user", principal_id=user_id, permissions=perms,
    ))


async def _seed():
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Prompt Org {suffix}")
        db.add(org); await db.flush()
        admin = _u(suffix, "Admin"); member = _u(suffix, "Member")
        db.add_all([admin, member]); await db.flush()
        for u, role in ((admin, "admin"), (member, "member")):
            db.add(Membership(user_id=u.id, organization_id=org.id, role=role))
        r = Role(organization_id=org.id, name=f"Admin {suffix}", permissions=["full_admin_access"], is_system=False)
        db.add(r); await db.flush()
        db.add(RoleAssignment(organization_id=org.id, role_id=r.id, principal_type="user", principal_id=admin.id))
        ds1 = DataSource(name=f"A1 {suffix}", organization_id=org.id, is_active=True, owner_user_id=admin.id)
        ds2 = DataSource(name=f"A2 {suffix}", organization_id=org.id, is_active=True, owner_user_id=admin.id)
        db.add_all([ds1, ds2]); await db.flush()
        await _grant(db, org.id, ds1.id, admin.id, ["view", "manage"])
        await _grant(db, org.id, ds2.id, admin.id, ["view", "manage"])
        await db.commit()
        return {"org": org.id, "admin": admin.id, "member": member.id, "ds1": ds1.id, "ds2": ds2.id}


@pytest.mark.asyncio
async def test_prompt_model_access_and_parameters():
    ids = await _seed()
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org"])
        admin = await db.get(User, ids["admin"])
        member = await db.get(User, ids["member"])

        async def member_sees(pid):
            lst = (await prompt_service.list_prompts(db, member, org))["prompts"]
            return pid in {p["id"] for p in lst}

        # global → all members
        g = await prompt_service.create_prompt(
            db, PromptCreate(title="Welcome", text="org-wide", scope="global", data_source_ids=[]), admin, org)
        assert await member_sees(g.id)
        print("[global] visible to all members")

        # member cannot create global
        denied = False
        try:
            await prompt_service.create_prompt(
                db, PromptCreate(text="x", scope="global"), member, org)
        except HTTPException as e:
            denied = e.status_code == 403
        assert denied
        print("[global] non-admin create denied")

        # agent prompt over two agents → need access to BOTH
        ap = await prompt_service.create_prompt(
            db, PromptCreate(title="Cross", text="two agents", scope="agent",
                             data_source_ids=[ids["ds1"], ids["ds2"]]), admin, org)
        assert not await member_sees(ap.id)
        await _grant(db, ids["org"], ids["ds1"], ids["member"], ["view"]); await db.commit()
        assert not await member_sees(ap.id), "one of two agents → still hidden"
        await _grant(db, ids["org"], ids["ds2"], ids["member"], ["view"]); await db.commit()
        assert await member_sees(ap.id), "both agents → visible"
        print("[multi-agent] ALL-access rule holds")

        # agent deactivated → hidden from non-owner
        ds_d = DataSource(name=f"D {uuid.uuid4().hex[:6]}", organization_id=ids["org"], is_active=True, owner_user_id=ids["admin"])
        db.add(ds_d); await db.flush()
        await _grant(db, ids["org"], ds_d.id, ids["admin"], ["view", "manage"])
        await _grant(db, ids["org"], ds_d.id, ids["member"], ["view"]); await db.commit()
        dp = await prompt_service.create_prompt(
            db, PromptCreate(title="D", text="deactivates", scope="agent", data_source_ids=[ds_d.id]), admin, org)
        assert await member_sees(dp.id)
        ds_d.is_active = False; await db.commit()
        assert not await member_sees(dp.id)
        print("[inactive] deactivated agent excluded from access")

        # agent prompt requires >=1 agent
        bad = False
        try:
            await prompt_service.create_prompt(db, PromptCreate(text="no agents", scope="agent"), admin, org)
        except HTTPException as e:
            bad = e.status_code == 400
        assert bad
        print("[agent] empty-agent prompt rejected")

        # parameters + mentions round-trip
        pp = await prompt_service.create_prompt(
            db, PromptCreate(
                title="Param", text="Summarize {{region}} for {{period}}", scope="global",
                parameters=[
                    PromptParameter(name="region", label="Region", type="enum", required=True, options=["EMEA", "AMER"]),
                    PromptParameter(name="period", type="date_range"),
                ],
                mentions=[{"type": "data_source", "id": ids["ds1"]}],
            ), admin, org)
        resp = await prompt_service.get_prompt_response(db, pp.id, admin, org)
        assert [p["name"] for p in resp["parameters"]] == ["region", "period"]
        assert resp["parameters"][0]["options"] == ["EMEA", "AMER"]
        assert resp["mentions"][0]["type"] == "data_source"
        await prompt_service.update_prompt(
            db, pp.id, PromptUpdate(parameters=[PromptParameter(name="only", type="text")]), admin, org)
        resp2 = await prompt_service.get_prompt_response(db, pp.id, admin, org)
        assert [p["name"] for p in resp2["parameters"]] == ["only"]
        print("[params] parameters + mentions round-trip OK")


@pytest.mark.asyncio
async def test_materialize_starters_idempotent():
    """The agent-creation path: a data source's conversation_starters become
    agent-scoped starter Prompts (idempotent), visible via starters_only."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Starter Org {suffix}")
        db.add(org); await db.flush()
        owner = _u(suffix, "Owner"); db.add(owner); await db.flush()
        ds = DataSource(name=f"Agent {suffix}", organization_id=org.id, is_active=True,
                        owner_user_id=owner.id,
                        conversation_starters=["What changed?", "Top customers"])
        db.add(ds); await db.flush()
        await _grant(db, org.id, ds.id, owner.id, ["view"]); await db.commit()

        created = await prompt_service.materialize_starters_for_data_source(db, ds)
        assert created == 2
        again = await prompt_service.materialize_starters_for_data_source(db, ds)
        assert again == 0, "idempotent"

        lst = (await prompt_service.list_prompts(db, owner, org, starters_only=True))["prompts"]
        texts = {p["text"] for p in lst}
        assert {"What changed?", "Top customers"} <= texts
        assert all(p["is_starter"] for p in lst)
        print(f"[materialize] created=2, idempotent, visible={len(texts)}")
