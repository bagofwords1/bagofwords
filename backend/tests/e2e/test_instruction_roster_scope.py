"""Roster-scoped instruction loading (the fix for "trivial turn over many agents
loads every agent's always-on rules").

The agent scopes the standing <instructions> block to the roster's focus by
setting ``InstructionContextBuilder.data_source_ids``:

  - full attached scope  → every attached agent's always-on rules load ("all"
    mode: few agents),
  - empty list           → GLOBAL always-on rules only, agent-attached ones
    deferred ("pick" mode: many agents, nothing picked yet),
  - focus subset         → globals + the picked/loaded agents' rules ("focus").

Global (no-agent) instructions always load regardless of scope; only
agent-attached ones are bounded.
"""
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

_SQLITE_DB = (Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite").resolve()


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _new_admin(create_user, login_user, whoami):
    email = f"rscope_{uuid.uuid4().hex[:6]}@test.com"
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
async def test_agent_always_instruction_respects_roster_scope(
    create_user, login_user, whoami, test_client, create_data_source
):
    from app.dependencies import async_session_maker
    from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

    token, org_id = _new_admin(create_user, login_user, whoami)
    ds = create_data_source(
        name="ds_scope", type="sqlite", config={"database": str(_SQLITE_DB)},
        credentials={}, user_token=token, org_id=org_id,
    )

    global_inst = _create(
        test_client, token, org_id,
        text="Always format currency with a leading symbol.", title="Currency",
        load_mode="always",
    )
    agent_inst = _create(
        test_client, token, org_id,
        text="For this agent, quarters start in February.", title="Agent quarters",
        load_mode="always", data_source_ids=[ds["id"]],
    )

    org = SimpleNamespace(id=org_id)

    # "pick" scope (empty list): globals only — the agent's always-on rule is
    # deferred even though it is 'always'.
    async with async_session_maker() as db:
        section = await InstructionContextBuilder(
            db, org, data_source_ids=[]
        ).build(query="hello")
    loaded = {it.id for it in section.items}
    assert global_inst["id"] in loaded, "global always-on rule must always load"
    assert agent_inst["id"] not in loaded, "agent always-on rule must NOT load until its agent is in play"

    # "focus" scope (agent picked): both load.
    async with async_session_maker() as db:
        section = await InstructionContextBuilder(
            db, org, data_source_ids=[ds["id"]]
        ).build(query="hello")
    loaded = {it.id for it in section.items}
    assert global_inst["id"] in loaded
    assert agent_inst["id"] in loaded, "agent always-on rule must load once its agent is focused"
