"""Deleting an agent must not widen the reach of its content.

An instruction attached to an agent (data source) is visible only to that
agent's members and loads only into that agent's AI context. "Global" is
defined as "attached to no agent", so if a delete merely drops the
association rows, every private instruction silently becomes global: readable
by every org member and injected into every other agent's prompt.

Contract (instructions, saved queries/entities and eval test cases alike):
- Content attached ONLY to the deleted agent is deleted with it.
- Content shared with another agent keeps its remaining scope.
- Global / agent-less content is untouched.
- The agent's instruction folders go away with it (and, on Postgres, no
  longer block the delete via the folder -> agent foreign key).
"""
import uuid
from types import SimpleNamespace

import pytest


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _create_instruction(test_client, token, org_id, text, data_source_ids):
    resp = test_client.post(
        "/api/instructions",
        json={
            "text": text,
            "status": "published",
            "category": "general",
            "load_mode": "always",
            "data_source_ids": data_source_ids,
        },
        headers=_auth(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _visible_ids(test_client, token, org_id):
    resp = test_client.get(
        "/api/instructions",
        params={
            "include_global": "true",
            "include_own": "true",
            "include_drafts": "true",
            "include_archived": "true",
            "limit": 200,
        },
        headers=_auth(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    return {item["id"] for item in resp.json()["items"]}


def _mkdir(test_client, token, org_id, name, data_source_id):
    resp = test_client.post(
        "/api/instructions/directories",
        json={"name": name, "data_source_id": data_source_id, "parent_id": None},
        headers=_auth(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _place(test_client, token, org_id, instruction_id, directory_id, data_source_id):
    resp = test_client.put(
        f"/api/instructions/{instruction_id}/directory",
        json={"directory_id": directory_id, "data_source_id": data_source_id},
        headers=_auth(token, org_id),
    )
    assert resp.status_code == 200, resp.json()


def _create_entity(test_client, token, org_id, title, data_source_ids):
    resp = test_client.post(
        "/api/entities/global",
        json={
            "type": "model",
            "title": title,
            "slug": f"{title.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}",
            "code": "def generate_df(ds_clients, excel_files):\n    import pandas as pd\n    return pd.DataFrame({'x': [1]})\n",
            "data_source_ids": data_source_ids,
        },
        headers=_auth(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()["id"]


def _create_case(test_client, token, org_id, suite_id, name, data_source_ids):
    resp = test_client.post(
        f"/api/tests/suites/{suite_id}/cases",
        json={
            "name": name,
            "prompt_json": {"content": "how many rows?"},
            "expectations_json": {},
            "data_source_ids_json": data_source_ids,
        },
        headers=_auth(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()["id"]


def _suite_cases(test_client, token, org_id, suite_id):
    resp = test_client.get(
        "/api/tests/cases", params={"suite_id": suite_id, "limit": 200}, headers=_auth(token, org_id)
    )
    assert resp.status_code == 200, resp.json()
    return {c["id"]: c for c in resp.json()}


async def _always_loaded_ids(org_id, data_source_ids):
    """What the planner would actually load for a report on these agents."""
    from app.dependencies import async_session_maker
    from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

    async with async_session_maker() as db:
        builder = InstructionContextBuilder(db, SimpleNamespace(id=org_id))
        rows = await builder.load_always_instructions(data_source_ids=data_source_ids)
        return {str(r.id) for r in rows}


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_deleting_agent_does_not_leak_its_content(
    test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source, delete_data_source,
):
    admin = bootstrap_admin("del_agent")
    token, org_id = admin["token"], admin["org_id"]
    # A regular member who is NOT a member of either private agent.
    outsider = invite_user_to_org(org_id=org_id, admin_token=token)

    doomed = sqlite_data_source(name=f"doomed_{uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id)
    survivor = sqlite_data_source(name=f"survivor_{uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id)
    doomed_id, survivor_id = doomed["id"], survivor["id"]

    only_doomed = [
        _create_instruction(test_client, token, org_id, f"private rule {i}", [doomed_id])["id"]
        for i in range(3)
    ]
    shared = _create_instruction(test_client, token, org_id, "shared rule", [doomed_id, survivor_id])["id"]
    survivor_only = _create_instruction(test_client, token, org_id, "survivor rule", [survivor_id])["id"]
    global_one = _create_instruction(test_client, token, org_id, "global rule", [])["id"]

    folder = _mkdir(test_client, token, org_id, "Private folder", doomed_id)
    _place(test_client, token, org_id, only_doomed[0], folder["id"], doomed_id)

    # Saved queries (entities) and eval cases follow the same rule.
    entity_only = _create_entity(test_client, token, org_id, "Doomed only query", [doomed_id])
    entity_shared = _create_entity(test_client, token, org_id, "Shared query", [doomed_id, survivor_id])
    entity_survivor = _create_entity(test_client, token, org_id, "Survivor query", [survivor_id])

    resp = test_client.post(
        "/api/tests/suites",
        json={"name": f"Doomed suite {uuid.uuid4().hex[:6]}", "data_source_id": doomed_id},
        headers=_auth(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    suite_id = resp.json()["id"]
    case_only = _create_case(test_client, token, org_id, suite_id, "doomed case", [doomed_id])
    case_shared = _create_case(test_client, token, org_id, suite_id, "shared case", [doomed_id, survivor_id])
    case_agentless = _create_case(test_client, token, org_id, suite_id, "agent-less case", [])
    assert set(_suite_cases(test_client, token, org_id, suite_id)) == {case_only, case_shared, case_agentless}

    # Sanity: before the delete the outsider only sees the global instruction,
    # and the survivor agent's context does not include the doomed agent's rules.
    assert _visible_ids(test_client, outsider["token"], org_id) & set(only_doomed) == set()
    assert global_one in _visible_ids(test_client, outsider["token"], org_id)
    before_ctx = await _always_loaded_ids(org_id, [survivor_id])
    assert before_ctx & set(only_doomed) == set()
    assert {shared, survivor_only, global_one} <= before_ctx

    delete_data_source(data_source_id=doomed_id, user_token=token, org_id=org_id)

    # The outsider must still not see the doomed agent's private instructions.
    outsider_sees = _visible_ids(test_client, outsider["token"], org_id)
    assert outsider_sees & set(only_doomed) == set(), "private instructions leaked to a non-member"
    assert global_one in outsider_sees

    # Even the admin no longer has them: they were deleted with the agent.
    admin_sees = _visible_ids(test_client, token, org_id)
    assert admin_sees & set(only_doomed) == set()
    for iid in only_doomed:
        resp = test_client.get(f"/api/instructions/{iid}", headers=_auth(token, org_id))
        assert resp.status_code == 404, resp.json()

    # Shared / other-agent / global instructions are untouched, and the shared
    # one keeps only its remaining agent (it did not become global).
    assert {shared, survivor_only, global_one} <= admin_sees
    resp = test_client.get(f"/api/instructions/{shared}", headers=_auth(token, org_id))
    assert resp.status_code == 200, resp.json()
    assert [ds["id"] for ds in resp.json()["data_sources"]] == [survivor_id]
    assert shared not in _visible_ids(test_client, outsider["token"], org_id)

    # The survivor agent's AI context is unchanged.
    after_ctx = await _always_loaded_ids(org_id, [survivor_id])
    assert after_ctx & set(only_doomed) == set(), "private instructions leaked into another agent's context"
    assert {shared, survivor_only, global_one} <= after_ctx

    # Global scope gained nothing: the counts endpoint's global figure is the
    # same set as before, i.e. exactly the one global instruction we created.
    counts = test_client.get("/api/instructions/counts", headers=_auth(token, org_id)).json()
    assert counts["global"] == 1, counts
    assert doomed_id not in counts["by_agent"]

    # The agent's folders are gone with it (its scope no longer exists).
    tree = test_client.get(
        "/api/instructions/directories",
        params={"data_source_id": doomed_id},
        headers=_auth(token, org_id),
    )
    assert tree.status_code == 200, tree.json()
    assert tree.json()["directories"] == []

    # Saved queries: the doomed-only one is gone, the shared one keeps only the
    # survivor, the survivor-only one is untouched.
    resp = test_client.get(f"/api/entities/{entity_only}", headers=_auth(token, org_id))
    assert resp.status_code == 404, resp.json()
    resp = test_client.get(f"/api/entities/{entity_shared}", headers=_auth(token, org_id))
    assert resp.status_code == 200, resp.json()
    assert [ds["id"] for ds in resp.json()["data_sources"]] == [survivor_id]
    resp = test_client.get(f"/api/entities/{entity_survivor}", headers=_auth(token, org_id))
    assert resp.status_code == 200, resp.json()
    assert [ds["id"] for ds in resp.json()["data_sources"]] == [survivor_id]
    listed = {e["id"] for e in test_client.get("/api/entities", params={"limit": 500}, headers=_auth(token, org_id)).json()}
    assert entity_only not in listed
    assert {entity_shared, entity_survivor} <= listed

    # Eval cases: the doomed-only case is deleted, the shared case now targets
    # only the survivor, the agent-less case is untouched. The suite itself is
    # kept as org-level content (existing behaviour).
    cases = _suite_cases(test_client, token, org_id, suite_id)
    assert case_only not in cases
    assert cases[case_shared]["data_source_ids_json"] == [survivor_id]
    assert cases[case_agentless]["data_source_ids_json"] in ([], None)
    suites = {s["id"]: s for s in test_client.get("/api/tests/suites", headers=_auth(token, org_id)).json()}
    assert suite_id in suites
    assert suites[suite_id]["data_source_id"] is None


@pytest.mark.e2e
def test_deleting_agent_with_folders_succeeds(
    test_client, bootstrap_admin, sqlite_data_source, delete_data_source,
):
    """Folders hard-reference the agent; nested folders must not block the delete."""
    admin = bootstrap_admin("del_folders")
    token, org_id = admin["token"], admin["org_id"]
    ds = sqlite_data_source(name=f"foldered_{uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id)

    parent = _mkdir(test_client, token, org_id, "Parent", ds["id"])
    resp = test_client.post(
        "/api/instructions/directories",
        json={"name": "Child", "data_source_id": ds["id"], "parent_id": parent["id"]},
        headers=_auth(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    instr = _create_instruction(test_client, token, org_id, "filed rule", [ds["id"]])
    _place(test_client, token, org_id, instr["id"], resp.json()["id"], ds["id"])

    result = delete_data_source(data_source_id=ds["id"], user_token=token, org_id=org_id)
    assert result.get("message"), result

    listing = test_client.get("/api/data_sources", headers=_auth(token, org_id))
    assert listing.status_code == 200
    assert ds["id"] not in {d["id"] for d in listing.json()}
