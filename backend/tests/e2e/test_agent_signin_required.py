"""Agents a person must sign in to, end to end through a real completion run.

Contract:
  * An agent backed by a per-user connection (auth_policy user_required) that
    THIS user has not signed in to, and that has no service-account fallback
    for them, is listed to the planner in <agents_needing_signin> — never as a
    schema it could query. Without the block the planner does not know the
    agent exists and answers from the wrong agent or says there is no data.
  * A user who CAN use the agent (here: the owner, through the system
    fallback) gets no such block; the agent is an ordinary one for them.
  * request_agent_signin shows a Connect card only for agents that really
    need this user's sign-in, and ends the turn with the planner's message;
    any other id is rejected back to the planner, which keeps going.

The LLM boundary is stubbed (records prompts, scripts the planner's tool
calls); routes, services, context building, the tool and the agent loop run
real.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from tests.e2e.test_smart_instructions_planner_prompt import env, llm  # noqa: F401 (shared planner fixtures)

pytestmark = pytest.mark.e2e

_CHINOOK = (Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite").resolve()
_BLOCK = re.compile(r"<agents_needing_signin[^>]*>(.*?)</agents_needing_signin>", re.S)


def _source(client, env, create_data_source, refresh_schema, bulk_update_tables, *, name, auth_policy):
    ds = create_data_source(name=name, type="sqlite", config={"database": str(_CHINOOK)}, credentials={},
                            auth_policy=auth_policy, user_token=env["token"], org_id=env["org_id"])
    refresh_schema(data_source_id=ds["id"], user_token=env["token"], org_id=env["org_id"])
    bulk_update_tables(data_source_id=ds["id"], action="activate", user_token=env["token"], org_id=env["org_id"])
    r = client.put(f"/api/data_sources/{ds['id']}", json={"is_public": True}, headers=env["headers"])
    assert r.status_code == 200, r.text
    return ds


def _member(client, create_user, login_user, env):
    org = env["org_id"]
    email = f"signin-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post(f"/api/organizations/{org}/members", headers=env["headers"],
                    json={"organization_id": org, "email": email, "role": "member"})
    assert r.status_code == 200, r.text
    user = create_user(email=email)
    token = login_user(user["email"], user["password"])
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org)}


def _ask(client, headers, llm, prompt, script):
    """Auto report (no agents pinned) + one turn. Returns (report_id, planner prompts)."""
    llm["planner"].clear()
    llm["base"] = None
    llm["script"] = script
    r = client.post("/api/reports", headers=headers, json={"title": "signin", "files": [], "data_sources": []})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    r = client.post(f"/api/reports/{rid}/completions", headers=headers, json={"prompt": {"content": prompt}})
    assert r.status_code == 200, r.text
    assert llm["planner"], "the run never reached the planner"
    return rid, list(llm["planner"])


def _signin_blocks(prompt: str) -> list[str]:
    # The tool description also names the tag in prose; real blocks hold <agent>.
    return [b for b in _BLOCK.findall(prompt) if "<agent " in b]


def _tool_executions(client, headers, rid, name):
    r = client.get(f"/api/reports/{rid}/completions", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    completions = body.get("completions", body) if isinstance(body, dict) else body
    out = []
    for c in completions:
        for b in c.get("completion_blocks") or []:
            te = b.get("tool_execution") or {}
            if te.get("tool_name") == name:
                out.append((c, te))
    return out


@pytest.fixture
def sources(test_client, env, create_data_source, refresh_schema, bulk_update_tables):
    per_user = _source(test_client, env, create_data_source, refresh_schema, bulk_update_tables,
                       name=f"Sales {uuid.uuid4().hex[:4]}", auth_policy="user_required")
    shared = _source(test_client, env, create_data_source, refresh_schema, bulk_update_tables,
                     name=f"Inventory {uuid.uuid4().hex[:4]}", auth_policy="system_only")
    return per_user, shared


def test_unsigned_agent_is_listed_for_signin_not_as_schema(test_client, env, llm, sources, create_user, login_user):
    per_user, shared = sources
    member = _member(test_client, create_user, login_user, env)

    _, prompts = _ask(test_client, member, llm, "What were sales last quarter?", script=[])

    blocks = _signin_blocks(prompts[0])
    assert len(blocks) == 1, "planner must be told which agents need the user's sign-in"
    assert per_user["id"] in blocks[0]
    assert shared["id"] not in blocks[0], "a usable agent is never listed as needing sign-in"
    # Outside the block the per-user agent must not appear at all: no schema,
    # no roster line the planner could try to query.
    outside = _BLOCK.sub("", prompts[0])
    assert per_user["id"] not in outside and per_user["name"] not in outside
    assert shared["name"] in outside, "the usable agent keeps its normal context"


def test_user_who_can_use_the_agent_gets_no_signin_block(test_client, env, llm, sources):
    per_user, _ = sources
    # The owner reaches the per-user agent through the system fallback.
    _, prompts = _ask(test_client, env["headers"], llm, "What were sales last quarter?", script=[])
    assert not any(_signin_blocks(p) for p in prompts)
    assert per_user["name"] in prompts[0], "the agent is an ordinary, queryable one for them"


def test_request_agent_signin_shows_connect_and_ends_turn(test_client, env, llm, sources, create_user, login_user):
    per_user, _ = sources
    member = _member(test_client, create_user, login_user, env)
    message = f"Connect the Sales agent to answer this. {uuid.uuid4().hex[:6]}"

    rid, prompts = _ask(test_client, member, llm, "What were sales last quarter?", script=[
        ("request_agent_signin", {"agent_ids": [per_user["id"]], "message": message}),
        ("request_agent_signin", {"agent_ids": [per_user["id"]], "message": "must not be reached"}),
    ])

    assert len(prompts) == 1, "the sign-in request ends the turn; the planner is not called again"
    runs = _tool_executions(test_client, member, rid, "request_agent_signin")
    assert len(runs) == 1
    completion, te = runs[0]
    result = te.get("result_json") or {}
    assert result.get("success") is True
    assert [a["id"] for a in result.get("agents", [])] == [per_user["id"]]
    assert message in (completion.get("completion") or {}).get("content", "") or any(
        message in (b.get("content") or "") for b in completion.get("completion_blocks") or []
    ), "the planner's message is the turn's reply"


def test_request_agent_signin_rejects_agents_that_need_no_signin(test_client, env, llm, sources, create_user, login_user):
    per_user, shared = sources
    member = _member(test_client, create_user, login_user, env)

    rid, prompts = _ask(test_client, member, llm, "How many tracks do we stock?", script=[
        ("request_agent_signin", {"agent_ids": [shared["id"], str(uuid.uuid4())], "message": "Connect it."}),
    ])

    runs = _tool_executions(test_client, member, rid, "request_agent_signin")
    assert len(runs) == 1
    result = runs[0][1].get("result_json") or {}
    assert result.get("success") is False and result.get("agents") == []
    assert set(result.get("rejected_ids") or []) >= {shared["id"]}
    assert len(prompts) >= 2, "a rejected request does not end the turn; the planner continues"

    # The owner can use the per-user agent, so for them it is rejected too.
    rid, _ = _ask(test_client, env["headers"], llm, "Sales?", script=[
        ("request_agent_signin", {"agent_ids": [per_user["id"]], "message": "Connect it."}),
    ])
    result = _tool_executions(test_client, env["headers"], rid, "request_agent_signin")[0][1].get("result_json") or {}
    assert result.get("success") is False and per_user["id"] in result.get("rejected_ids", [])
