"""RBAC matrix for instruction suggest/create/delete — community AND EE grants.

The product rule under test (revised 2026-08-04):

- A plain member (community baseline) or a minimal "query" role holds NO
  manage_instructions anywhere. For now the instruction tools are HIDDEN from
  them in every mode — open suggestion capture is deferred. The
  publication-side safety stays pinned regardless: were a suggestion of
  theirs ever staged, it must not self-publish, and they may retract their
  OWN unpublished suggestion but nothing else.
- A per-agent manager (EE-style scoped grant) may create on THEIR agent only.
  Selecting an agent they don't manage fails with an error that NAMES the
  agent; re-selecting only the permitted agent succeeds (the "fix the modal
  selection" flow).
"""
import uuid
from types import SimpleNamespace

import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def world(test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source, grant_resource):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]
    ds_a = sqlite_data_source(name=f"agent-alpha-{uuid.uuid4().hex[:4]}", user_token=admin["token"], org_id=org_id)
    ds_b = sqlite_data_source(name=f"agent-beta-{uuid.uuid4().hex[:4]}", user_token=admin["token"], org_id=org_id)
    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])  # no grants at all
    manager_b = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    grant_resource(
        resource_type="data_source", resource_id=ds_b["id"],
        principal_type="user", principal_id=manager_b["user_id"],
        permissions=["manage_instructions"], user_token=admin["token"], org_id=org_id,
    )
    return {"org_id": org_id, "admin": admin, "member": member,
            "manager_b": manager_b, "ds_a": ds_a, "ds_b": ds_b}


# ── The harness suggest path (AI tool) ──────────────────────────────────────

async def _run_create_tool(*, user_id, org_id, mode, ds_ids=None):
    from app.dependencies import async_session_maker
    from app.ai.tools.implementations.create_instruction import CreateInstructionTool

    async with async_session_maker() as db:
        ctx = {
            "db": db,
            "user": SimpleNamespace(id=user_id),
            "organization": SimpleNamespace(id=org_id),
            "mode": mode,
        }
        if ds_ids is not None:
            ctx["report"] = SimpleNamespace(
                id=str(uuid.uuid4()),
                data_sources=[SimpleNamespace(id=d) for d in ds_ids],
            )
        end = None
        async for evt in CreateInstructionTool().run_stream(
            {"text": "Members' suggestions must reach review, not publication.",
             "category": "general", "confidence": 0.9, "evidence": "User confirmed."},
            ctx,
        ):
            if evt.type == "tool.error":
                pytest.fail(f"tool errored: {evt.payload}")
            if evt.type == "tool.end":
                end = evt
        return end.payload["output"]


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_member_staged_suggestion_never_self_publishes(test_client, world):
    """Publication-side safety, pinned independently of the catalog filter:
    if a member's capture ever reaches the tool (the filter currently hides it,
    but any future re-open or direct path must stay safe), the result is a
    pending suggestion — never live/published text."""
    out = await _run_create_tool(
        user_id=world["member"]["user_id"], org_id=world["org_id"], mode="knowledge",
    )
    assert out["success"] is True, out
    iid = out["instruction_id"]

    # Admin can see it in review, and it is NOT published.
    r = test_client.get(f"/api/instructions/{iid}",
                        headers=_hdr(world["admin"]["token"], world["org_id"]))
    assert r.status_code == 200, r.text
    assert r.json()["status"] != "published", (
        "a member's suggestion must not publish itself: " + r.text
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_instruction_tools_hidden_from_member_in_every_mode(world):
    """Current product rule: a user with no manage_instructions anywhere gets
    create/edit_instruction stripped from the catalog in ALL modes (training,
    knowledge and chat alike) — open suggestion capture is deferred. Unrelated
    tools must survive the filter untouched."""
    from app.dependencies import async_session_maker
    from app.ai.agent_v2 import AgentV2

    class _Tool:  # minimal stand-in for planner catalog entries
        def __init__(self, name): self.name = name

    async with async_session_maker() as db:
        from sqlalchemy import select
        from app.models.organization import Organization
        org = (await db.execute(select(Organization).where(
            Organization.id == world["org_id"]))).scalars().first()

        for mode, expect_present in (("training", False), ("knowledge", False), ("chat", False)):
            agent = AgentV2.__new__(AgentV2)  # no full init: we only exercise the filter
            agent.db = db
            agent.organization = org
            agent.mode = mode
            agent.head_completion = SimpleNamespace(user=SimpleNamespace(id=world["member"]["user_id"]))
            agent.planner = SimpleNamespace(tool_catalog=[
                _Tool("create_instruction"), _Tool("edit_instruction"), _Tool("create_data"),
            ])

            class _Meta:
                def __init__(self, perms): self.required_permissions = perms
            agent.registry = SimpleNamespace(get_metadata=lambda n: _Meta(
                ["manage_instructions"] if n in ("create_instruction", "edit_instruction") else []
            ))

            await agent._apply_tool_permission_filter()
            names = {t.name for t in agent.planner.tool_catalog}
            if expect_present:
                assert {"create_instruction", "edit_instruction"} <= names, (mode, names)
            else:
                assert "create_instruction" not in names and "edit_instruction" not in names, (mode, names)
            assert "create_data" in names


# ── The HTTP paths ──────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_member_create_on_agent_403s_naming_the_missing_permission(test_client, world):
    """A zero-grant member is stopped at the org gate — naming a specific agent
    would be meaningless (they lack the permission everywhere), but the denial
    must say WHICH permission is missing instead of a bare "Permission denied".
    The named-agent message belongs to the partial-authority case below."""
    r = test_client.post(
        "/api/instructions",
        json={"text": "member tries to attach to an agent", "status": "published",
              "category": "general", "data_source_ids": [world["ds_a"]["id"]]},
        headers=_hdr(world["member"]["token"], world["org_id"]),
    )
    assert r.status_code == 403, r.text
    detail = r.json().get("detail", "")
    assert "manage_instructions" in detail, f"403 must name the permission: {detail}"


@pytest.mark.e2e
def test_manager_modal_flow_fails_on_foreign_agent_then_succeeds_on_own(test_client, world):
    """The instruction-modal flow: manager of agent-beta selects [alpha, beta]
    -> 403 naming alpha; deselects alpha -> create succeeds on beta alone."""
    mgr = world["manager_b"]
    r_both = test_client.post(
        "/api/instructions",
        json={"text": "rule for both agents", "status": "published",
              "category": "general",
              "data_source_ids": [world["ds_a"]["id"], world["ds_b"]["id"]]},
        headers=_hdr(mgr["token"], world["org_id"]),
    )
    assert r_both.status_code == 403, r_both.text
    assert world["ds_a"]["name"] in r_both.json().get("detail", "")

    r_own = test_client.post(
        "/api/instructions",
        json={"text": "rule for my own agent", "status": "published",
              "category": "general", "data_source_ids": [world["ds_b"]["id"]]},
        headers=_hdr(mgr["token"], world["org_id"]),
    )
    assert r_own.status_code == 200, r_own.text


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_member_can_retract_own_suggestion_but_not_others(test_client, world):
    """Suggesting is open, so retracting your own unpublished suggestion must be
    too. Someone ELSE's suggestion — or anything published — stays gated."""
    out = await _run_create_tool(
        user_id=world["member"]["user_id"], org_id=world["org_id"], mode="knowledge",
    )
    iid = out["instruction_id"]

    # Another plain member cannot delete it.
    other = world["manager_b"]  # has manage on ds_b only; suggestion is global
    r_other = test_client.delete(f"/api/instructions/{iid}",
                                 headers=_hdr(other["token"], world["org_id"]))
    assert r_other.status_code == 403, r_other.text

    # The author can.
    r_own = test_client.delete(f"/api/instructions/{iid}",
                               headers=_hdr(world["member"]["token"], world["org_id"]))
    assert r_own.status_code == 200, r_own.text


@pytest.mark.e2e
def test_member_cannot_delete_published_instruction(test_client, world):
    """Owner-retract stops at publication: once live, deletion changes agent
    behaviour and needs manage authority even for the author."""
    admin = world["admin"]
    r = test_client.post(
        "/api/instructions",
        json={"text": "published org rule", "status": "published",
              "category": "general", "data_source_ids": []},
        headers=_hdr(admin["token"], world["org_id"]),
    )
    assert r.status_code == 200, r.text
    iid = r.json()["id"]

    r_del = test_client.delete(f"/api/instructions/{iid}",
                               headers=_hdr(world["member"]["token"], world["org_id"]))
    assert r_del.status_code == 403, r_del.text
