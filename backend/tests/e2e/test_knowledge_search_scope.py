"""GET /api/knowledge/search?data_source_id=… — the /agents search scoped to one agent.

Scoped: only the agent's own instructions — not global ones (the tree lists
those in their own group) and never another agent's — and no agent rows. Unscoped behavior is unchanged.
"""
import uuid

import pytest


@pytest.fixture
def two_agents(create_user, login_user, whoami, create_data_source, create_instruction):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = str(whoami(token)["organizations"][0]["id"])
    tag = uuid.uuid4().hex[:8]

    def _ds(name):
        return create_data_source(
            name=f"{name}-{tag}", type="sqlite", config={"database": ":memory:"},
            credentials={}, user_token=token, org_id=org_id,
        )

    a, b = _ds("alpha"), _ds("beta")
    ids = {}
    for key, ds_ids in (("a", [a["id"]]), ("b", [b["id"]]), ("global", [])):
        ins = create_instruction(text=f"rule {tag} {key}", user_token=token, org_id=org_id,
                                 status="published", data_source_ids=ds_ids)
        ids[key] = ins["id"]
    return {"tag": tag, "a": a, "b": b, "ids": ids,
            "headers": {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}}


def _search(client, ctx, **params):
    r = client.get("/api/knowledge/search", params={"q": ctx["tag"], "limit": 50, **params},
                   headers=ctx["headers"])
    assert r.status_code == 200, r.text[:400]
    return r.json()


@pytest.mark.e2e
def test_unscoped_search_returns_every_match_and_agents(test_client, two_agents):
    body = _search(test_client, two_agents)
    found = {i["id"] for i in body["instructions"]}
    assert set(two_agents["ids"].values()) <= found
    assert {two_agents["a"]["id"], two_agents["b"]["id"]} <= {a["id"] for a in body["agents"]}


@pytest.mark.e2e
def test_scoped_search_keeps_only_the_agent_instructions(test_client, two_agents):
    body = _search(test_client, two_agents, data_source_id=two_agents["a"]["id"])
    found = {i["id"] for i in body["instructions"]}
    assert two_agents["ids"]["a"] in found
    assert two_agents["ids"]["global"] not in found
    assert two_agents["ids"]["b"] not in found
    assert body["agents"] == []
