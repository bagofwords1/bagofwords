"""
RBAC end-to-end coverage for the *agent-manager* tier — the user stories
behind making a `manage` grant a superset (see
``RESOURCE_PERM_IMPLIES`` in ``app/core/permission_resolver.py``).

World (a representative slice of "admin invites N users across M groups;
groups can create agents"):

    admin            — full_admin_access (bootstrap owner)
    group "analysts" — assigned a role with ONLY ``create_data_source``
    m1, m2           — members of "analysts" (inherit create_data_source)
    outsider         — plain member, NOT in the group (cannot create agents)

    agent1           — created by m1 → m1 owns it (per-DS `manage` grant)
    agent2           — created by m2 → m2 owns it
    agent_admin      — admin's own private agent

Each numbered test maps to a product user story. Stories 1/2/4 exercise the
new `manage` ⇒ {manage_instructions, create_entities, manage_evals} implication;
3/5 are list-scoping (must hold regardless of the implication).
"""
import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _instruction_body(text, ds_ids):
    return {"text": text, "status": "draft", "category": "general", "data_source_ids": ds_ids}


def _entity_body(title, ds_ids):
    return {
        "type": "model",
        "title": title,
        "slug": f"{title}-{(ds_ids[0] if ds_ids else 'global')[:6]}",
        "code": "select 1 as v",
        "data": {},
        "tags": [],
        "status": "draft",
        "data_source_ids": ds_ids,
    }


@pytest.fixture
def group_world(
    test_client,
    bootstrap_admin,
    invite_user_to_org,
    create_role,
    assign_role,
    create_group,
    add_user_to_group,
    sqlite_data_source,
):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]

    # A role that ONLY allows creating agents — assigned to a GROUP, so every
    # group member inherits the capability (the "groups can create agents" story).
    role = create_role(
        name="agent-creators",
        permissions=["create_data_source"],
        user_token=admin["token"],
        org_id=org_id,
    )
    assert role.status_code == 200, role.json()
    role_id = role.json()["id"]

    grp = create_group(name="analysts", user_token=admin["token"], org_id=org_id)
    assert grp.status_code == 200, grp.json()
    group_id = grp.json()["id"]

    asg = assign_role(
        role_id=role_id, principal_type="group", principal_id=group_id,
        user_token=admin["token"], org_id=org_id,
    )
    assert asg.status_code in (200, 201), asg.json()

    m1 = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    m2 = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    for m in (m1, m2):
        r = add_user_to_group(
            group_id=group_id, user_id=m["user_id"],
            user_token=admin["token"], org_id=org_id,
        )
        assert r.status_code in (200, 201), r.text

    outsider = invite_user_to_org(org_id=org_id, admin_token=admin["token"])

    # Group members create their own agents → each becomes owner (manage grant).
    agent1 = sqlite_data_source(name="agent1", user_token=m1["token"], org_id=org_id)
    agent2 = sqlite_data_source(name="agent2", user_token=m2["token"], org_id=org_id)
    agent_admin = sqlite_data_source(name="agent_admin", user_token=admin["token"], org_id=org_id)

    return {
        "org_id": org_id,
        "admin": admin,
        "m1": m1,
        "m2": m2,
        "outsider": outsider,
        "agent1": agent1,
        "agent2": agent2,
        "agent_admin": agent_admin,
    }


# ── Groups can create agents; non-group members can't ────────────────────────

@pytest.mark.e2e
def test_group_members_can_create_agents(test_client, group_world):
    """Group members inherit create_data_source and successfully create their
    own agents (the fixture creates agent1/agent2 as m1/m2 — a 403 there would
    fail fixture setup). Each creator becomes a member of their own agent."""
    org_id = group_world["org_id"]
    m1, m2 = group_world["m1"], group_world["m2"]
    assert group_world["agent1"]["id"] and group_world["agent2"]["id"]

    # The creator is auto-enrolled on the agent they created.
    r1 = test_client.get("/api/data_sources", headers=_hdr(m1["token"], org_id))
    assert r1.status_code == 200, r1.text
    assert group_world["agent1"]["id"] in {d["id"] for d in r1.json()}

    r2 = test_client.get("/api/data_sources", headers=_hdr(m2["token"], org_id))
    assert r2.status_code == 200, r2.text
    assert group_world["agent2"]["id"] in {d["id"] for d in r2.json()}


@pytest.mark.e2e
def test_story1_manager_can_edit_own_agent_instructions(test_client, group_world):
    """Story 1: a group member who created an agent can create AND edit
    instructions on that agent (manage ⇒ manage_instructions)."""
    org_id = group_world["org_id"]
    m1 = group_world["m1"]
    agent1_id = group_world["agent1"]["id"]

    # Create an instruction on their own agent.
    created = test_client.post(
        "/api/instructions",
        json=_instruction_body("m1 rule on agent1", [agent1_id]),
        headers=_hdr(m1["token"], org_id),
    )
    assert created.status_code == 200, created.text
    inst_id = created.json()["id"]

    # Edit it.
    edited = test_client.put(
        f"/api/instructions/{inst_id}",
        json={"text": "m1 edits its own rule"},
        headers=_hdr(m1["token"], org_id),
    )
    assert edited.status_code == 200, edited.text


@pytest.mark.e2e
def test_story2_manager_cannot_add_global_or_edit_others(test_client, group_world):
    """Story 2: a manager cannot author org-wide GLOBAL instructions, and
    cannot create/edit instructions on agents they don't manage."""
    org_id = group_world["org_id"]
    m1 = group_world["m1"]
    m2 = group_world["m2"]
    agent2_id = group_world["agent2"]["id"]

    # (a) Global instruction (no data source) → org-level only → 403.
    r = test_client.post(
        "/api/instructions/global",
        json=_instruction_body("m1 tries global", []),
        headers=_hdr(m1["token"], org_id),
    )
    assert r.status_code == 403, r.text

    # (b) Create an instruction on m2's agent → 403.
    r = test_client.post(
        "/api/instructions",
        json=_instruction_body("m1 writes on agent2", [agent2_id]),
        headers=_hdr(m1["token"], org_id),
    )
    assert r.status_code == 403, r.text

    # (c) m2 authors an instruction on their own agent; m1 cannot edit it.
    m2_inst = test_client.post(
        "/api/instructions",
        json=_instruction_body("m2 rule on agent2", [agent2_id]),
        headers=_hdr(m2["token"], org_id),
    )
    assert m2_inst.status_code == 200, m2_inst.text
    m2_inst_id = m2_inst.json()["id"]

    r = test_client.put(
        f"/api/instructions/{m2_inst_id}",
        json={"text": "m1 hijacks agent2 rule"},
        headers=_hdr(m1["token"], org_id),
    )
    assert r.status_code == 403, r.text


@pytest.mark.e2e
def test_story3_and_5_manager_only_sees_own_agents(test_client, group_world):
    """Stories 3 & 5: a user scoped to specific agents does not see other
    agents — neither in the /data_sources list nor in the /data_sources/active
    selector that backs the /agents page."""
    org_id = group_world["org_id"]
    m1 = group_world["m1"]
    agent1_id = group_world["agent1"]["id"]
    agent2_id = group_world["agent2"]["id"]
    agent_admin_id = group_world["agent_admin"]["id"]

    for path in ("/api/data_sources", "/api/data_sources/active?include_unconnected=true"):
        resp = test_client.get(path, headers=_hdr(m1["token"], org_id))
        assert resp.status_code == 200, resp.text
        ids = {d["id"] for d in resp.json()}
        assert agent1_id in ids, f"{path}: m1 should see their own agent"
        assert agent2_id not in ids, f"{path}: m1 must NOT see m2's agent"
        assert agent_admin_id not in ids, f"{path}: m1 must NOT see admin's agent"

    # The outsider (no agents) sees none of them.
    resp = test_client.get("/api/data_sources", headers=_hdr(group_world["outsider"]["token"], org_id))
    assert resp.status_code == 200, resp.text
    out_ids = {d["id"] for d in resp.json()}
    assert not ({agent1_id, agent2_id, agent_admin_id} & out_ids)


@pytest.mark.e2e
def test_story4_manager_can_add_entities_to_own_agent_only(test_client, group_world):
    """Story 4: a manager can add entities to an agent they manage
    (manage ⇒ create_entities), but not to agents they don't manage, and not
    org-wide global entities."""
    org_id = group_world["org_id"]
    m1 = group_world["m1"]
    agent1_id = group_world["agent1"]["id"]
    agent2_id = group_world["agent2"]["id"]

    # On their own agent → allowed.
    r = test_client.post(
        "/api/entities",
        json=_entity_body("ent_on_agent1", [agent1_id]),
        headers=_hdr(m1["token"], org_id),
    )
    assert r.status_code == 200, r.text

    # On m2's agent → denied.
    r = test_client.post(
        "/api/entities",
        json=_entity_body("ent_on_agent2", [agent2_id]),
        headers=_hdr(m1["token"], org_id),
    )
    assert r.status_code == 403, r.text

    # Global (no data source) entity → org-level only → denied.
    r = test_client.post(
        "/api/entities/global",
        json=_entity_body("ent_global", []),
        headers=_hdr(m1["token"], org_id),
    )
    assert r.status_code == 403, r.text


@pytest.mark.e2e
def test_expansion_manager_can_edit_tables_and_members_of_own_agent(test_client, group_world):
    """Expansion: the manage grant also covers the agent's tables and
    membership for the agent they own — scoped to that agent only."""
    org_id = group_world["org_id"]
    m1 = group_world["m1"]
    m2 = group_world["m2"]
    agent1_id = group_world["agent1"]["id"]
    agent2_id = group_world["agent2"]["id"]

    # Tables: allowed on own agent, denied on m2's.
    own = test_client.put(
        f"/api/data_sources/{agent1_id}/update_tables_status",
        json={"activate": [], "deactivate": []},
        headers=_hdr(m1["token"], org_id),
    )
    assert own.status_code != 403, own.text
    other = test_client.put(
        f"/api/data_sources/{agent2_id}/update_tables_status",
        json={"activate": [], "deactivate": []},
        headers=_hdr(m1["token"], org_id),
    )
    assert other.status_code == 403, other.text

    # Membership: m1 can add the outsider to agent1, but not to agent2.
    outsider_uid = group_world["outsider"]["user_id"]
    add_own = test_client.post(
        f"/api/data_sources/{agent1_id}/members",
        json={"principal_type": "user", "principal_id": outsider_uid},
        headers=_hdr(m1["token"], org_id),
    )
    assert add_own.status_code in (200, 201), add_own.text
    add_other = test_client.post(
        f"/api/data_sources/{agent2_id}/members",
        json={"principal_type": "user", "principal_id": outsider_uid},
        headers=_hdr(m1["token"], org_id),
    )
    assert add_other.status_code == 403, add_other.text


@pytest.mark.e2e
def test_expansion_manage_does_not_grant_create_agent(test_client, group_world):
    """Expansion: owning/managing an agent does NOT confer the org-level
    ability to create *new* agents — that's the separate create_data_source
    role. (Here m1 already has it via the group; the outsider, who manages
    nothing and isn't in the group, is denied.)"""
    org_id = group_world["org_id"]
    outsider = group_world["outsider"]
    r = test_client.post(
        "/api/data_sources",
        json={
            "name": "outsider_agent", "type": "sqlite",
            "config": {}, "credentials": {}, "auth_policy": "system_only",
            "generate_summary": False, "generate_conversation_starters": False,
            "generate_ai_rules": False,
        },
        headers=_hdr(outsider["token"], org_id),
    )
    assert r.status_code == 403, r.text
