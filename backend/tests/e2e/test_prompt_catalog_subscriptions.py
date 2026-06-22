"""Prompt catalog + scheduled-prompt subscriptions — app-logic validation.

Self-contained (no LLM, no live channel): seeds an org, a data source (agent),
and three users with different access, then exercises the catalog service:

  - visibility is scoped to agent access (all of a prompt's agents)
  - self-subscribe creates a ScheduledPrompt that runs AS the user
  - admin assign fans out to users, skipping those without agent access
  - assigning without `assign_prompts` is denied (403)

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      .venv/bin/python -m pytest tests/e2e/test_prompt_catalog_subscriptions.py -v -s
"""
import uuid
import asyncio

import pytest
from fastapi import HTTPException

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.data_source import DataSource
from app.models.resource_grant import ResourceGrant
from app.models.membership import Membership
from app.models.scheduled_prompt import ScheduledPrompt
from sqlalchemy import select

from app.services.prompt_catalog_service import prompt_catalog_service
from app.schemas.prompt_catalog_schema import (
    PromptCatalogCreate, SubscribeRequest, AssignRequest,
)


def _run(coro):
    return asyncio.run(coro)


def _user(suffix, name):
    return User(
        name=name, email=f"{name.lower()}-{suffix}@example.com",
        hashed_password="x", is_active=True, is_superuser=False, is_verified=True,
    )


async def _seed():
    """Return ids for org, ds, and three users:
    admin (manage+assign_prompts), member_ok (view), member_no (no access)."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Prompt Org {suffix}")
        db.add(org)
        await db.flush()

        admin = _user(suffix, "Admin")
        member_ok = _user(suffix, "MemberOk")
        member_no = _user(suffix, "MemberNo")
        db.add_all([admin, member_ok, member_no])
        await db.flush()

        # org memberships (org-wide assign expands over these)
        for u, role in ((admin, "admin"), (member_ok, "member"), (member_no, "member")):
            db.add(Membership(user_id=u.id, organization_id=org.id, role=role))

        ds = DataSource(name=f"Agent {suffix}", organization_id=org.id, is_active=True, owner_user_id=admin.id)
        db.add(ds)
        await db.flush()

        # admin: manage + assign_prompts on the agent
        db.add(ResourceGrant(
            organization_id=org.id, resource_type="data_source", resource_id=ds.id,
            principal_type="user", principal_id=admin.id,
            permissions=["view", "manage", "assign_prompts"],
        ))
        # member_ok: plain view access (membership on the agent)
        db.add(ResourceGrant(
            organization_id=org.id, resource_type="data_source", resource_id=ds.id,
            principal_type="user", principal_id=member_ok.id,
            permissions=["view"],
        ))
        # member_no: nothing
        await db.commit()

        return {
            "org_id": org.id, "ds_id": ds.id,
            "admin_id": admin.id, "member_ok_id": member_ok.id, "member_no_id": member_no.id,
        }


@pytest.mark.asyncio
async def test_catalog_visibility_subscribe_and_assign():
    ids = await _seed()

    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org_id"])
        admin = await db.get(User, ids["admin_id"])
        member_ok = await db.get(User, ids["member_ok_id"])
        member_no = await db.get(User, ids["member_no_id"])

        # ── admin authors an agent-scoped, published prompt on the agent ──
        prompt = await prompt_catalog_service.create_prompt(
            db,
            PromptCatalogCreate(
                title="Weekly Forecast", text="Summarize this week's sales and my customers",
                scope="agent", status="published", is_starter=True,
                default_cron="0 9 * * 1", default_channel="teams",
                data_source_ids=[ids["ds_id"]],
            ),
            current_user=admin, organization=org,
        )
        assert prompt.scope == "agent"
        print(f"[create] prompt {prompt.id} on agent {ids['ds_id']}")

        # ── visibility ──
        admin_list = await prompt_catalog_service.list_prompts(db, admin, org, sort="top")
        ok_list = await prompt_catalog_service.list_prompts(db, member_ok, org, sort="top")
        no_list = await prompt_catalog_service.list_prompts(db, member_no, org, sort="top")
        admin_ids = {p["id"] for p in admin_list["prompts"]}
        ok_ids = {p["id"] for p in ok_list["prompts"]}
        no_ids = {p["id"] for p in no_list["prompts"]}
        print(f"[visibility] admin={len(admin_ids)} member_ok={len(ok_ids)} member_no={len(no_ids)}")
        assert prompt.id in admin_ids, "admin should see the prompt"
        assert prompt.id in ok_ids, "member with agent access should see it"
        assert prompt.id not in no_ids, "member without agent access must NOT see it"

        # can_assign flag only for admin
        admin_row = next(p for p in admin_list["prompts"] if p["id"] == prompt.id)
        ok_row = next(p for p in ok_list["prompts"] if p["id"] == prompt.id)
        assert admin_row["can_assign"] is True
        assert ok_row["can_assign"] is False

        # ── self-subscribe (member_ok) ──
        sp = await prompt_catalog_service.subscribe(
            db, prompt.id,
            SubscribeRequest(cron_schedule="0 9 * * 1", channel="teams", run_mode="append"),
            current_user=member_ok, organization=org,
        )
        assert sp.user_id == member_ok.id
        assert sp.prompt_id == prompt.id
        assert sp.channel == "teams"
        print(f"[subscribe] member_ok sp={sp.id} runs_as={sp.user_id}")

        # subscriber_count now reflects the subscription
        ok_list2 = await prompt_catalog_service.list_prompts(db, admin, org, sort="top")
        admin_row2 = next(p for p in ok_list2["prompts"] if p["id"] == prompt.id)
        assert admin_row2["subscriber_count"] == 1

        # ── member_ok lacks assign_prompts → assign is denied ──
        denied = False
        try:
            await prompt_catalog_service.assign(
                db, prompt.id,
                AssignRequest(principal_type="user", principal_id=member_no.id,
                              cron_schedule="0 9 * * 1", channel="teams"),
                current_user=member_ok, organization=org,
            )
        except HTTPException as e:
            denied = e.status_code == 403
        assert denied, "member_ok without assign_prompts must be denied"
        print("[rbac] member_ok assign -> 403 (correct)")

        # ── admin assigns to org: member_no skipped (no agent access) ──
        result = await prompt_catalog_service.assign(
            db, prompt.id,
            AssignRequest(principal_type="org", cron_schedule="0 9 * * 1",
                          channel="teams", run_mode="new_report"),
            current_user=admin, organization=org,
        )
        print(f"[assign-org] created={result['created']} skipped={result['skipped']}")
        # admin + member_ok can access the agent; member_no cannot
        assert result["created"] == 2, "admin and member_ok should get subscriptions"
        assert result["skipped"] >= 1, "member_no should be skipped (no agent access)"

        # all created subscriptions point at the prompt and carry the channel + run_mode
        rows = await db.execute(
            select(ScheduledPrompt).filter(ScheduledPrompt.prompt_id == prompt.id)
        )
        subs = list(rows.scalars().all())
        assert all(s.channel == "teams" for s in subs)
        assert any(s.run_mode == "new_report" for s in subs)
        print(f"[done] total subscriptions for prompt: {len(subs)}")
