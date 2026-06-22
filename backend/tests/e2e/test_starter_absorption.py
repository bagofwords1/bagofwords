"""Conversation starters absorbed into the prompt catalog.

Materializes a data source's `conversation_starters` strings into agent-scoped
starter Prompts, and verifies they surface via the catalog (starters_only) and
are idempotent.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      .venv/bin/python -m pytest tests/e2e/test_starter_absorption.py -v -s
"""
import uuid
import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.data_source import DataSource
from app.models.resource_grant import ResourceGrant
from app.services.prompt_catalog_service import prompt_catalog_service


@pytest.mark.asyncio
async def test_starters_materialize_and_list():
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Starter Org {suffix}")
        db.add(org)
        await db.flush()
        user = User(name=f"Owner {suffix}", email=f"owner-{suffix}@example.com",
                    hashed_password="x", is_active=True, is_verified=True)
        db.add(user)
        await db.flush()
        ds = DataSource(
            name=f"Agent {suffix}", organization_id=org.id, is_active=True,
            owner_user_id=user.id,
            conversation_starters=["What changed this week?", "Top customers by revenue"],
        )
        db.add(ds)
        await db.flush()
        # owner needs agent access to see the agent-scoped starters
        db.add(ResourceGrant(
            organization_id=org.id, resource_type="data_source", resource_id=ds.id,
            principal_type="user", principal_id=user.id, permissions=["view"],
        ))
        await db.commit()

        created = await prompt_catalog_service.materialize_starters_for_data_source(db, ds)
        print(f"[materialize] created={created}")
        assert created == 2

        # idempotent: running again creates nothing
        again = await prompt_catalog_service.materialize_starters_for_data_source(db, ds)
        print(f"[idempotent] created_again={again}")
        assert again == 0

        # they surface as starters in the catalog for a user with agent access
        listing = await prompt_catalog_service.list_prompts(db, user, org, starters_only=True)
        texts = {p["text"] for p in listing["prompts"]}
        print(f"[catalog] starters visible={len(texts)}")
        assert "What changed this week?" in texts
        assert "Top customers by revenue" in texts
        assert all(p["is_starter"] for p in listing["prompts"])
