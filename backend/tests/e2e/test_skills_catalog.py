"""E2E test: skills are advertised in the prompt as a catalog, not force-loaded.

InstructionContextBuilder.build() returns an InstructionsSection whose:
  - `items` hold force-loaded instructions (always / keyword-matched intelligent),
    and must EXCLUDE skills, and
  - `skills` hold the advertised catalog (short id + title + description) for
    every published skill in scope.
The rendered string carries an <available_skills> block.
"""
import uuid
from types import SimpleNamespace

import pytest


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _new_admin(create_user, login_user, whoami):
    email = f"catalog_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    me = whoami(token)
    return token, me["organizations"][0]["id"]


def _create(test_client, token, org_id, **fields):
    resp = test_client.post(
        "/api/instructions", json={"status": "published", **fields}, headers=_auth(token, org_id)
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_skill_advertised_not_loaded(create_user, login_user, whoami, test_client):
    from app.dependencies import async_session_maker
    from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

    token, org_id = _new_admin(create_user, login_user, whoami)

    skill = _create(
        test_client, token, org_id,
        text="One-line summary of the cohort skill.\n\nLONGDETAILBODY step 1; step 2; step 3.",
        title="Cohort analysis skill", kind="skill",
    )
    normal = _create(
        test_client, token, org_id,
        text="Always-loaded normal rule.", title="Rule", kind="instruction", load_mode="always",
    )

    async with async_session_maker() as db:
        builder = InstructionContextBuilder(db, SimpleNamespace(id=org_id))
        section = await builder.build(query=None)

    item_ids = {it.id for it in section.items}
    skill_catalog_ids = {sk.id for sk in section.skills}

    # Skill is advertised in the catalog, not in the force-loaded items.
    assert skill["id"] in skill_catalog_ids, section.skills
    assert skill["id"] not in item_ids
    # Normal 'always' instruction IS force-loaded.
    assert normal["id"] in item_ids

    rendered = section.render()
    assert "<available_skills>" in rendered
    assert skill["id"][:8] in rendered
    assert "Cohort analysis skill" in rendered
    # Only the one-line description is advertised — the full body is withheld
    # (the agent must call read_skill to get it).
    assert "One-line summary of the cohort skill." in rendered
    assert "LONGDETAILBODY" not in rendered


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_explicit_description_used_in_catalog(create_user, login_user, whoami, test_client):
    """An explicit description overrides the first-line-of-text fallback."""
    from app.dependencies import async_session_maker
    from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

    token, org_id = _new_admin(create_user, login_user, whoami)
    _create(
        test_client, token, org_id,
        text="FIRSTLINEFALLBACK should not be advertised.\n\nbody...",
        title="Skill with description",
        description="A crisp human-written blurb.",
        kind="skill",
    )

    async with async_session_maker() as db:
        builder = InstructionContextBuilder(db, SimpleNamespace(id=org_id))
        section = await builder.build(query=None)

    rendered = section.render()
    assert "A crisp human-written blurb." in rendered
    assert "FIRSTLINEFALLBACK" not in rendered
