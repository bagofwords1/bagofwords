"""create_list / edit_list — the training-mode tools that define a list's schema.

Contracts:
- Training-only: neither tool is in the chat or knowledge catalog.
- Lists follow the owning agent: only a user who can MANAGE the agent may
  create or edit a list through the tool (a viewer is refused before any
  review card is shown). Agents must be attached to the report.
- The schema is validated with the same contract as the REST API; an invalid
  schema returns errors and shows no card.
- Nothing is written until the user approves on the card; a decline carries
  the user's feedback back to the agent.
- Approval creates/updates the list live (same as the editor), and flags the
  run (``lists_changed``) so the submit_<slug> tool is (re)registered.
- edit_list preserves field ids (renames are not removals), classifies the
  change like the editor does, and a breaking edit bumps the version.
"""
import asyncio
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.e2e


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _fields(extra=None):
    return [
        {"name": "counterparty", "type": "string", "required": True, "description": "Other party"},
        {"name": "annual_value", "type": "number", "unit": "USD", "description": "Per 12 months"},
        {"name": "currency", "type": "enum", "enum": ["USD", "EUR"], "description": "Currency"},
        {"name": "renewal_date", "type": "date", "description": "Next renewal"},
    ] + (extra or [])


def _create_input(name="Contracts", **over):
    body = {"name": name, "description": "One row per contract", "fields": _fields(),
            "key_field": "counterparty", "require_evidence": True, "reason": "User asked for a contracts table."}
    body.update(over)
    return body


def _db_url():
    return (os.environ["TEST_DATABASE_URL"]
            .replace("sqlite://", "sqlite+aiosqlite://", 1)
            .replace("postgresql://", "postgresql+asyncpg://", 1))


def run_async(fn):
    async def _run():
        engine = create_async_engine(_db_url())
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as db:
                return await fn(db, maker)
        finally:
            await engine.dispose()
    return asyncio.run(_run())


def drive(world, tool_name, tool_input, *, actor="admin", report_key="report", decision=None):
    """Run the tool as AgentV2 does (runtime_ctx with the shared session and a
    session maker). When the review card shows, answer it with ``decision``
    (``None`` asserts no card is shown). Returns (output, events, runtime_ctx)."""
    from app.ai.registry import ToolRegistry
    from app.ai.tools.confirmation import resolve_confirmation
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User
    from app.services.tool_confirmation_service import ToolConfirmationService

    async def _fn(db, maker):
        report = await db.get(Report, world[report_key]["id"])
        user = await db.get(User, world[actor]["user_id"])
        org = await db.get(Organization, world["org_id"])
        ctx = {"db": db, "session_maker": maker, "report": report, "user": user, "organization": org,
               "mode": "training", "tool_call_id": str(uuid.uuid4())}
        tool = ToolRegistry().get(tool_name)
        events = []
        async for ev in tool.run_stream(tool_input, ctx):
            events.append(ev)
            if ev.type == "tool.confirmation":
                assert decision is not None, "unexpected review card"
                cid = ev.payload["confirmation_id"]
                async with maker() as s2:
                    await ToolConfirmationService().resolve(
                        s2, confirmation_id=cid, approved=decision["approved"], remember=False,
                        user_id=str(user.id), response=decision.get("response"))
                resolve_confirmation(cid, {"approved": decision["approved"], "remember": False,
                                           "response": decision.get("response")})
        end = [e for e in events if e.type == "tool.end"][-1]
        return end.payload["output"], events, {k: v for k, v in ctx.items() if k == "lists_changed"}
    return run_async(_fn)


def registered_tools(world, report_key="report", actor="admin"):
    """What AgentV2 would register for this report right now (build_list_tools)."""
    from app.ai.tools.list_tool_registry import build_list_tools
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User

    async def _fn(db, maker):
        report = await db.get(Report, world[report_key]["id"])
        user = await db.get(User, world[actor]["user_id"])
        org = await db.get(Organization, world["org_id"])
        descriptors, routing = await build_list_tools(db, report, user, org)
        return {d["name"]: d for d in descriptors}, routing
    return run_async(_fn)


def lists_of(test_client, world, agent_key="agent", actor="admin"):
    r = test_client.get(f"/api/data_sources/{world[agent_key]['id']}/lists", headers=_h(world[actor]["token"], world["org_id"]))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def world(bootstrap_admin, invite_user_to_org, sqlite_data_source, grant_resource, create_report):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]
    agent = sqlite_data_source(name=f"Sales Ops {uuid.uuid4().hex[:4]}", user_token=admin["token"], org_id=org_id)
    other_agent = sqlite_data_source(name=f"Legal {uuid.uuid4().hex[:4]}", user_token=admin["token"], org_id=org_id)

    viewer = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    manager = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    for who, perms in ((viewer, ["view"]), (manager, ["manage"])):
        r = grant_resource(resource_type="data_source", resource_id=agent["id"], principal_type="user",
                           principal_id=who["user_id"], permissions=perms, user_token=admin["token"], org_id=org_id)
        assert r.status_code in (200, 201), r.text

    report = create_report(title="Training", user_token=admin["token"], org_id=org_id, data_sources=[agent["id"]])
    two_agents = create_report(title="Training 2", user_token=admin["token"], org_id=org_id,
                               data_sources=[agent["id"], other_agent["id"]])
    return {"org_id": org_id, "admin": admin, "viewer": viewer, "manager": manager,
            "agent": agent, "other_agent": other_agent, "report": report, "two_agents": two_agents}


# ── catalog ────────────────────────────────────────────────────────────────

def test_list_schema_tools_are_training_only():
    from app.ai.registry import ToolRegistry
    r = ToolRegistry()
    for mode in ("training", "chat", "knowledge"):
        names = set()
        for pt in ("action", "research"):
            names |= {t["name"] for t in r.get_catalog_for_plan_type(pt, mode=mode)}
        assert ({"create_list", "edit_list"} <= names) == (mode == "training"), mode


# ── create_list ─────────────────────────────────────────────────────────────

def test_viewer_cannot_create_a_list_and_sees_no_card(test_client, world):
    out, events, ctx = drive(world, "create_list", _create_input(), actor="viewer")
    assert out["success"] is False and out["status"] == "permission_denied"
    assert not any(e.type == "tool.confirmation" for e in events)
    assert lists_of(test_client, world) == []
    assert not ctx.get("lists_changed")


@pytest.mark.parametrize("bad,hint", [
    ({"fields": [{"name": "Not Snake", "type": "string"}]}, "snake_case"),
    ({"fields": [{"name": "c", "type": "enum", "enum": []}]}, "enum"),
    ({"fields": [{"name": "a"}, {"name": "a"}]}, "duplicate"),
    ({"key_field": "zzz"}, "key_field"),
    ({"fields": _fields([{"name": "flag", "type": "boolean"}]), "key_field": "flag"}, "key_field"),
])
def test_invalid_schema_returns_errors_without_a_card(test_client, world, bad, hint):
    out, events, _ = drive(world, "create_list", _create_input(**bad))
    assert out["success"] is False and out["status"] == "invalid", out
    assert out["errors"] and any(hint in e for e in out["errors"]), out["errors"]
    assert not any(e.type == "tool.confirmation" for e in events)
    assert lists_of(test_client, world) == []


def test_decline_with_feedback_creates_nothing_and_returns_the_feedback(test_client, world):
    out, events, ctx = drive(world, "create_list", _create_input(),
                             decision={"approved": False, "response": {"feedback": "add a signing date"}})
    assert out["success"] is False and out["status"] == "rejected"
    assert out["feedback"] == "add a signing date"
    assert any(e.type == "tool.confirmation" for e in events)
    assert lists_of(test_client, world) == []
    assert not ctx.get("lists_changed")


@pytest.mark.parametrize("actor", ["admin", "manager"])
def test_approval_creates_the_list_live_and_registers_its_submit_tool(test_client, world, actor):
    before, _ = registered_tools(world)
    out, events, ctx = drive(world, "create_list", _create_input(), actor=actor, decision={"approved": True})
    assert out["success"] is True and out["status"] == "created", out
    assert out["agent_id"] == world["agent"]["id"]
    assert out["tool_name"].startswith("submit_") and out["table_name"].endswith(".lists." + out["slug"])
    assert out["key_field"] == "counterparty" and len(out["fields"]) == 4
    assert ctx.get("lists_changed") is True

    # The review card carried the schema the user approved.
    card = next(e for e in events if e.type == "tool.confirmation").payload
    assert card["tool_name"] == "create_list" and card["action"] == "create"
    assert [f["name"] for f in card["fields"]] == [f["name"] for f in _fields()]
    assert next(f for f in card["fields"] if f["key"])["name"] == "counterparty"

    # Visible through the API exactly like a list made in the editor …
    lists = lists_of(test_client, world)
    assert [l["id"] for l in lists] == [out["list_id"]]
    assert lists[0]["require_evidence"] is True and lists[0]["version"] == 1
    # … and the next registration pass offers its submit tool with the compiled schema.
    after, routing = registered_tools(world)
    assert out["tool_name"] not in before and out["tool_name"] in after
    assert routing[out["tool_name"]]["list_id"] == out["list_id"]
    props = after[out["tool_name"]]["schema"]["properties"]["records"]["items"]["properties"]["fields"]["properties"]
    assert set(props) == {f["name"] for f in _fields()}


def test_duplicate_name_is_refused_before_review(test_client, world):
    out, _, _ = drive(world, "create_list", _create_input(), decision={"approved": True})
    assert out["status"] == "created"
    again, events, _ = drive(world, "create_list", _create_input(name="  contracts "))
    assert again["success"] is False and again["status"] == "duplicate_name"
    assert again["list_id"] == out["list_id"]
    assert not any(e.type == "tool.confirmation" for e in events)
    assert len(lists_of(test_client, world)) == 1


def test_list_cap_is_enforced_before_review(test_client, world):
    from app.schemas.agent_list import MAX_LISTS_PER_AGENT
    for i in range(MAX_LISTS_PER_AGENT):
        r = test_client.post(f"/api/data_sources/{world['agent']['id']}/lists",
                             json={"name": f"L{i}", "fields": [{"name": "a"}]},
                             headers=_h(world["admin"]["token"], world["org_id"]))
        assert r.status_code == 201, r.text
    out, events, _ = drive(world, "create_list", _create_input())
    assert out["success"] is False and out["status"] == "limit_reached"
    assert not any(e.type == "tool.confirmation" for e in events)


def test_several_agents_require_an_explicit_agent_id(test_client, world):
    out, events, _ = drive(world, "create_list", _create_input(), report_key="two_agents")
    assert out["success"] is False and out["status"] == "needs_agent"
    assert {a["id"] for a in out["agents"]} == {world["agent"]["id"], world["other_agent"]["id"]}
    assert not any(e.type == "tool.confirmation" for e in events)

    unknown, _, _ = drive(world, "create_list", _create_input(agent_id=str(uuid.uuid4())), report_key="two_agents")
    assert unknown["status"] == "agent_not_found" and unknown["agents"]

    out, _, _ = drive(world, "create_list", _create_input(agent_id=world["other_agent"]["id"]),
                      report_key="two_agents", decision={"approved": True})
    assert out["status"] == "created" and out["agent_id"] == world["other_agent"]["id"]
    assert [l["id"] for l in lists_of(test_client, world, "other_agent")] == [out["list_id"]]
    assert lists_of(test_client, world) == []


def test_manager_of_one_agent_cannot_create_on_the_other(test_client, world):
    # The manager holds `manage` on `agent` only: on the two-agent report the
    # other agent is not offered, and naming it is refused.
    out, _, _ = drive(world, "create_list", _create_input(), actor="manager", report_key="two_agents",
                      decision={"approved": True})
    assert out["status"] == "created" and out["agent_id"] == world["agent"]["id"]
    denied, events, _ = drive(world, "create_list", _create_input(name="Other", agent_id=world["other_agent"]["id"]),
                              actor="manager", report_key="two_agents")
    assert denied["success"] is False and denied["status"] == "agent_not_found"
    assert not any(e.type == "tool.confirmation" for e in events)
    assert lists_of(test_client, world, "other_agent") == []


# ── edit_list ───────────────────────────────────────────────────────────────

@pytest.fixture
def created(test_client, world):
    out, _, _ = drive(world, "create_list", _create_input(), decision={"approved": True})
    assert out["status"] == "created"
    return out


def test_viewer_cannot_edit(test_client, world, created):
    out, events, _ = drive(world, "edit_list", {"list_id": created["list_id"], "description": "x"}, actor="viewer")
    assert out["success"] is False and out["status"] == "permission_denied"
    assert not any(e.type == "tool.confirmation" for e in events)
    assert lists_of(test_client, world)[0]["description"] == "One row per contract"


def test_unknown_list_lists_the_available_ones(world, created):
    out, events, _ = drive(world, "edit_list", {"list_id": "nope", "description": "x"})
    assert out["success"] is False and out["status"] == "list_not_found"
    assert created["list_name"] in out["message"]
    assert not any(e.type == "tool.confirmation" for e in events)


def test_additive_edit_keeps_version_and_field_ids_and_is_flagged_additive(test_client, world, created):
    before = lists_of(test_client, world)[0]
    edit = {
        "list_id": created["list_name"],  # resolve by name, not id
        "add_fields": [{"name": "signed_on", "type": "date", "description": "Signature date"}],
        "update_fields": [{"name": "annual_value", "new_name": "yearly_value", "description": "Per year excl. VAT"}],
        "reason": "track the signing date",
    }
    out, events, ctx = drive(world, "edit_list", edit, decision={"approved": True})
    assert out["success"] is True and out["status"] == "updated", out
    assert out["change"] == "additive" and out["version"] == before["version"]
    assert ctx.get("lists_changed") is True

    card = next(e for e in events if e.type == "tool.confirmation").payload
    assert card["action"] == "edit" and card["change"] == "additive"
    by_name = {f["name"]: f for f in card["fields"]}
    assert by_name["signed_on"]["status"] == "added" and by_name["yearly_value"]["status"] == "changed"
    assert by_name["counterparty"]["status"] == "unchanged"

    after = lists_of(test_client, world)[0]
    old_ids = {f["name"]: f["id"] for f in before["fields"]}
    new_ids = {f["name"]: f["id"] for f in after["fields"]}
    assert new_ids["yearly_value"] == old_ids["annual_value"]  # rename kept the id
    assert set(new_ids) == set(old_ids) - {"annual_value"} | {"yearly_value", "signed_on"}
    assert after["version"] == before["version"]
    # The submit tool now exposes the new columns.
    tools, _ = registered_tools(world)
    props = tools[created["tool_name"]]["schema"]["properties"]["records"]["items"]["properties"]["fields"]["properties"]
    assert {"signed_on", "yearly_value"} <= set(props) and "annual_value" not in props


def test_breaking_edit_bumps_version_and_the_card_says_so(test_client, world, created):
    before = lists_of(test_client, world)[0]
    out, events, _ = drive(world, "edit_list", {"list_id": created["list_id"], "remove_fields": ["renewal_date"]},
                           decision={"approved": True})
    assert out["status"] == "updated" and out["change"] == "breaking"
    assert out["version"] == before["version"] + 1
    card = next(e for e in events if e.type == "tool.confirmation").payload
    assert card["change"] == "breaking"
    assert next(f for f in card["fields"] if f["name"] == "renewal_date")["status"] == "removed"
    after = lists_of(test_client, world)[0]
    assert after["version"] == before["version"] + 1
    assert "renewal_date" not in {f["name"] for f in after["fields"]}


def test_declined_edit_changes_nothing(test_client, world, created):
    before = lists_of(test_client, world)[0]
    out, _, ctx = drive(world, "edit_list", {"list_id": created["list_id"], "remove_fields": ["currency"]},
                        decision={"approved": False, "response": {"feedback": "keep currency"}})
    assert out["success"] is False and out["status"] == "rejected" and out["feedback"] == "keep currency"
    assert lists_of(test_client, world)[0] == before
    assert not ctx.get("lists_changed")


def test_no_op_edit_and_bad_field_refs_show_no_card(test_client, world, created):
    same, events, _ = drive(world, "edit_list", {"list_id": created["list_id"], "name": "Contracts"})
    assert same["status"] == "no_change" and same["success"] is False
    bad, events2, _ = drive(world, "edit_list", {"list_id": created["list_id"], "remove_fields": ["ghost"]})
    assert bad["status"] == "invalid" and any("ghost" in e for e in bad["errors"])
    assert not any(e.type == "tool.confirmation" for e in events + events2)
