"""Prompt model (focused): multi-agent association, access scoping, global scope,
inactive-data-source handling, and parameters.

  - global prompts are visible to every org member
  - agent prompts are visible only to users with access to ALL active agents
  - inactive agents are excluded from access (agent prompt with only inactive
    agents is owner/admin-only)
  - parameters (+ mentions) round-trip through create/get

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      .venv/bin/python -m pytest tests/e2e/test_prompt_model_access.py -v -s
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

from app.services.prompt_catalog_service import prompt_catalog_service
from app.schemas.prompt_catalog_schema import PromptCatalogCreate, PromptParameter, PromptCatalogUpdate


def _u(suffix, name):
    return User(name=f"{name} {suffix}", email=f"{name.lower()}-{suffix}@example.com",
                hashed_password="x", is_active=True, is_verified=True)


async def _grant_ds(db, org_id, ds_id, user_id, perms):
    db.add(ResourceGrant(
        organization_id=org_id, resource_type="data_source", resource_id=ds_id,
        principal_type="user", principal_id=user_id, permissions=perms,
    ))


async def _seed():
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Model Org {suffix}")
        db.add(org); await db.flush()
        admin = _u(suffix, "Admin"); member = _u(suffix, "Member")
        db.add_all([admin, member]); await db.flush()
        for u, role in ((admin, "admin"), (member, "member")):
            db.add(Membership(user_id=u.id, organization_id=org.id, role=role))
        # full_admin role for admin
        r = Role(organization_id=org.id, name=f"Admin {suffix}", permissions=["full_admin_access"], is_system=False)
        db.add(r); await db.flush()
        db.add(RoleAssignment(organization_id=org.id, role_id=r.id, principal_type="user", principal_id=admin.id))

        ds1 = DataSource(name=f"A1 {suffix}", organization_id=org.id, is_active=True, owner_user_id=admin.id)
        ds2 = DataSource(name=f"A2 {suffix}", organization_id=org.id, is_active=True, owner_user_id=admin.id)
        ds_off = DataSource(name=f"Off {suffix}", organization_id=org.id, is_active=False, owner_user_id=admin.id)
        db.add_all([ds1, ds2, ds_off]); await db.flush()
        await _grant_ds(db, org.id, ds1.id, admin.id, ["view", "manage"])
        await _grant_ds(db, org.id, ds2.id, admin.id, ["view", "manage"])
        await _grant_ds(db, org.id, ds_off.id, admin.id, ["view", "manage"])
        await db.commit()
        return {"org": org.id, "admin": admin.id, "member": member.id,
                "ds1": ds1.id, "ds2": ds2.id, "ds_off": ds_off.id}


@pytest.mark.asyncio
async def test_global_agent_inactive_and_parameters():
    ids = await _seed()
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org"])
        admin = await db.get(User, ids["admin"])
        member = await db.get(User, ids["member"])

        # ── global prompt: visible to every member ──
        g = await prompt_catalog_service.create_prompt(
            db, PromptCatalogCreate(title="Welcome", text="Org-wide help", scope="global",
                                    data_source_ids=[]), admin, org)
        member_ids = {p["id"] for p in (await prompt_catalog_service.list_prompts(db, member, org))["prompts"]}
        print(f"[global] member sees global: {g.id in member_ids}")
        assert g.id in member_ids

        # member cannot create a global prompt
        denied = False
        try:
            await prompt_catalog_service.create_prompt(
                db, PromptCatalogCreate(title="x", text="y", scope="global", data_source_ids=[]), member, org)
        except HTTPException as e:
            denied = e.status_code == 403
        assert denied, "non-admin must not create global prompts"

        # ── multi-agent: need access to ALL active agents ──
        ap = await prompt_catalog_service.create_prompt(
            db, PromptCatalogCreate(title="Cross", text="uses two agents", scope="agent",
                                    data_source_ids=[ids["ds1"], ids["ds2"]]), admin, org)
        def member_sees(pid):
            return pid
        async def _member_sees(pid):
            lst = (await prompt_catalog_service.list_prompts(db, member, org))["prompts"]
            return pid in {p["id"] for p in lst}

        assert not await _member_sees(ap.id), "no access to either agent → hidden"
        await _grant_ds(db, ids["org"], ids["ds1"], ids["member"], ["view"]); await db.commit()
        assert not await _member_sees(ap.id), "access to only one of two agents → still hidden"
        await _grant_ds(db, ids["org"], ids["ds2"], ids["member"], ["view"]); await db.commit()
        assert await _member_sees(ap.id), "access to BOTH agents → visible"
        print("[multi-agent] ALL-access rule holds")

        # ── agent deactivated later: prompt with only inactive agents is owner/admin-only ──
        ds_d = DataSource(name=f"Deact {uuid.uuid4().hex[:6]}", organization_id=ids["org"],
                          is_active=True, owner_user_id=ids["admin"])
        db.add(ds_d); await db.flush()
        await _grant_ds(db, ids["org"], ds_d.id, ids["admin"], ["view", "manage"])
        await _grant_ds(db, ids["org"], ds_d.id, ids["member"], ["view"]); await db.commit()
        offp = await prompt_catalog_service.create_prompt(
            db, PromptCatalogCreate(title="Deact", text="agent will deactivate", scope="agent",
                                    data_source_ids=[ds_d.id]), admin, org)
        assert await _member_sees(offp.id), "member sees it while the agent is active"
        # deactivate the agent
        ds_d.is_active = False; await db.commit()
        assert not await _member_sees(offp.id), "after deactivation: hidden from non-owner"
        admin_ids = {p["id"] for p in (await prompt_catalog_service.list_prompts(db, admin, org))["prompts"]}
        assert offp.id in admin_ids, "admin still sees it"
        print("[inactive] deactivated agents excluded from access")

        # ── parameters + mentions round-trip ──
        pp = await prompt_catalog_service.create_prompt(
            db, PromptCatalogCreate(
                title="Param", text="Summarize {{region}} for {{period}}", scope="global",
                data_source_ids=[],
                parameters=[
                    PromptParameter(name="region", label="Region", type="enum", required=True, options=["EMEA", "AMER"]),
                    PromptParameter(name="period", label="Period", type="date_range"),
                ],
                mentions=[{"type": "data_source", "id": ids["ds1"]}],
            ), admin, org)
        resp = await prompt_catalog_service.get_prompt_response(db, pp.id, admin, org)
        print(f"[params] params={[p['name'] for p in resp['parameters']]} mentions={len(resp['mentions'])}")
        assert [p["name"] for p in resp["parameters"]] == ["region", "period"]
        assert resp["parameters"][0]["options"] == ["EMEA", "AMER"]
        assert resp["mentions"][0]["type"] == "data_source"

        # update parameters
        await prompt_catalog_service.update_prompt(
            db, pp.id, PromptCatalogUpdate(parameters=[PromptParameter(name="only", type="text")]), admin, org)
        resp2 = await prompt_catalog_service.get_prompt_response(db, pp.id, admin, org)
        assert [p["name"] for p in resp2["parameters"]] == ["only"]
        print("[params] update round-trip OK")
