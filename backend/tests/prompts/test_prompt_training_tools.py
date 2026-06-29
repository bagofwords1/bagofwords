"""Training-mode prompt tools: create_prompt / search_prompts / edit_prompt.

Exercises the tools through their run_stream against the real PromptService,
including the manage_agent (`manage` grant) authorization gate.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      .venv/bin/python -m pytest tests/prompts/test_prompt_training_tools.py -v -s
"""
import uuid
import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.data_source import DataSource
from app.models.resource_grant import ResourceGrant
from app.models.membership import Membership
from app.models.role import Role
from app.models.role_assignment import RoleAssignment

from app.ai.tools.implementations.create_prompt import CreatePromptTool
from app.ai.tools.implementations.edit_prompt import EditPromptTool
from app.ai.tools.implementations.search_prompts import SearchPromptsTool


async def _final(tool, tool_input, ctx):
    """Drain run_stream and return the terminal event's payload."""
    last = None
    async for ev in tool.run_stream(tool_input, ctx):
        last = ev
    return last


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
        org = Organization(name=f"PromptTool Org {suffix}")
        db.add(org); await db.flush()
        # manager: only a per-agent `manage` grant (no admin role) — the manage_agent tier
        manager = _u(suffix, "Manager")
        # outsider: member with no grant on the agent
        outsider = _u(suffix, "Outsider")
        db.add_all([manager, outsider]); await db.flush()
        for u in (manager, outsider):
            db.add(Membership(user_id=u.id, organization_id=org.id, role="member"))
        ds = DataSource(name=f"Agent {suffix}", organization_id=org.id, is_active=True, owner_user_id=manager.id)
        db.add(ds); await db.flush()
        await _grant(db, org.id, ds.id, manager.id, ["view", "manage"])
        await db.commit()
        return {"org": org.id, "manager": manager.id, "outsider": outsider.id, "ds": ds.id}


@pytest.mark.asyncio
async def test_prompt_training_tools_end_to_end():
    ids = await _seed()
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org"])
        manager = await db.get(User, ids["manager"])
        outsider = await db.get(User, ids["outsider"])

        mgr_ctx = {"db": db, "organization": org, "user": manager}
        out_ctx = {"db": db, "organization": org, "user": outsider}

        # --- create (manager, agent-scoped) ---
        ev = await _final(CreatePromptTool(), {
            "text": "Show monthly revenue by product category for the last 12 months.",
            "title": "Monthly revenue by category",
            "is_starter": True,
            "data_source_ids": [ids["ds"]],
        }, mgr_ctx)
        out = ev.payload["output"]
        assert out["success"] is True, out
        assert out["scope"] == "agent"
        assert ids["ds"] in out["data_source_ids"]
        assert out["is_starter"] is True
        prompt_id = out["prompt_id"]
        print(f"[create] ok -> {prompt_id} attached to agent {ids['ds']}")

        # --- create denied for a non-manager of the agent ---
        ev = await _final(CreatePromptTool(), {
            "text": "Outsider should not be able to attach this.",
            "data_source_ids": [ids["ds"]],
        }, out_ctx)
        out = ev.payload["output"]
        assert out["success"] is False and out["rejected_reason"] == "permission_denied", out
        print("[create] correctly denied for non-manager (manage_agent gate)")

        # --- search finds it ---
        ev = await _final(SearchPromptsTool(), {"query": ["revenue"]}, mgr_ctx)
        out = ev.payload["output"]
        assert out["success"] is True
        assert prompt_id in {p["id"] for p in out["prompts"]}, out
        print(f"[search] found {out['total']} prompt(s) for 'revenue'")

        # --- search starters_only ---
        ev = await _final(SearchPromptsTool(), {"starters_only": True}, mgr_ctx)
        assert prompt_id in {p["id"] for p in ev.payload["output"]["prompts"]}
        print("[search] starters_only returns the starter")

        # --- edit (rename + toggle starter off) ---
        ev = await _final(EditPromptTool(), {
            "prompt_id": prompt_id,
            "title": "Monthly revenue by category (v2)",
            "is_starter": False,
        }, mgr_ctx)
        out = ev.payload["output"]
        assert out["success"] is True and out["title"].endswith("(v2)"), out
        print("[edit] renamed + starter toggled off")

        # --- edit denied for outsider ---
        ev = await _final(EditPromptTool(), {"prompt_id": prompt_id, "title": "hijack"}, out_ctx)
        out = ev.payload["output"]
        assert out["success"] is False and out["rejected_reason"] == "permission_denied", out
        print("[edit] correctly denied for non-manager")

        # --- confirm edit persisted via search ---
        ev = await _final(SearchPromptsTool(), {"query": ["v2"]}, mgr_ctx)
        titles = {p["title"] for p in ev.payload["output"]["prompts"]}
        assert any(t and t.endswith("(v2)") for t in titles), titles
        print("[verify] edit persisted")
