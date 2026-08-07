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


# ── Re-scoping: which agents may be ADDED to an existing instruction/eval ────


@pytest.mark.e2e
def test_manager_can_widen_scope_only_to_agents_they_manage(
    test_client, group_world, sqlite_data_source
):
    """Adding a second agent to an existing instruction is allowed only when
    the manager manages the agent being added.

    The gate is on the resulting scope, not on the delta: authority over an
    instruction is the intersection of its agents, so widening onto an agent
    you don't manage would hand you edit rights over a rule that now applies
    to someone else's agent.
    """
    world = group_world
    org_id, m1 = world["org_id"], world["m1"]

    # A SECOND agent m1 owns (creating one makes the creator its manager).
    agent1b = sqlite_data_source(name="agent1b", user_token=m1["token"], org_id=org_id)

    created = test_client.post(
        "/api/instructions",
        json=_instruction_body("Exclude refunded orders.", [world["agent1"]["id"]]),
        headers=_hdr(m1["token"], org_id),
    )
    assert created.status_code == 200, created.text
    iid = created.json()["id"]

    # Widen onto another agent m1 manages → allowed.
    widen_ok = test_client.put(
        f"/api/instructions/{iid}",
        json={"data_source_ids": [world["agent1"]["id"], agent1b["id"]]},
        headers=_hdr(m1["token"], org_id),
    )
    assert widen_ok.status_code == 200, widen_ok.text
    assert {d["id"] for d in widen_ok.json()["data_sources"]} == {
        world["agent1"]["id"], agent1b["id"]
    }

    # Widen onto m2's agent → refused, and the scope must be unchanged.
    widen_bad = test_client.put(
        f"/api/instructions/{iid}",
        json={"data_source_ids": [world["agent1"]["id"], world["agent2"]["id"]]},
        headers=_hdr(m1["token"], org_id),
    )
    assert widen_bad.status_code == 403, widen_bad.text

    after = test_client.get(f"/api/instructions/{iid}", headers=_hdr(m1["token"], org_id))
    assert after.status_code == 200, after.text
    assert {d["id"] for d in after.json()["data_sources"]} == {
        world["agent1"]["id"], agent1b["id"]
    }, "a refused re-scope must not partially apply"


@pytest.mark.e2e
def test_eval_case_scope_follows_manage_evals_per_agent(
    test_client, group_world, sqlite_data_source
):
    """Same rule for eval cases, keyed on manage_evals rather than
    manage_instructions."""
    world = group_world
    org_id, m1 = world["org_id"], world["m1"]
    agent1b = sqlite_data_source(name="agent1b_ev", user_token=m1["token"], org_id=org_id)

    # Homed on an agent m1 manages. A suite with no home is an org-wide shelf
    # and takes org-level manage_evals, which m1 does not hold.
    suite = test_client.post(
        "/api/tests/suites",
        json={"name": "m1 suite", "description": None,
              "data_source_id": world["agent1"]["id"]},
        headers=_hdr(m1["token"], org_id),
    )
    assert suite.status_code == 200, suite.text

    spec = {"spec_version": 1, "rules": [], "order_mode": "flexible"}
    case = test_client.post(
        f"/api/tests/suites/{suite.json()['id']}/cases",
        json={"name": "c1", "prompt_json": {"text": "revenue?"},
              "expectations_json": spec,
              "data_source_ids_json": [world["agent1"]["id"]]},
        headers=_hdr(m1["token"], org_id),
    )
    assert case.status_code == 200, case.text
    cid = case.json()["id"]

    ok = test_client.patch(
        f"/api/tests/cases/{cid}",
        json={"data_source_ids_json": [world["agent1"]["id"], agent1b["id"]]},
        headers=_hdr(m1["token"], org_id),
    )
    assert ok.status_code == 200, ok.text

    bad = test_client.patch(
        f"/api/tests/cases/{cid}",
        json={"data_source_ids_json": [world["agent1"]["id"], world["agent2"]["id"]]},
        headers=_hdr(m1["token"], org_id),
    )
    assert bad.status_code == 403, bad.text


# ── Read-after-write for org-tier admins ─────────────────────────────────────


@pytest.mark.e2e
def test_org_instruction_admin_can_read_back_what_they_wrote(
    test_client, group_world, create_role, assign_role, invite_user_to_org
):
    """An org-level manage_instructions holder with NO agent grants must be able
    to GET the instruction they just created on someone else's agent.

    The write gate resolves through ORG_PERM_IMPLIES_RESOURCE, so the create and
    the edit both succeed. If the view gate only consults membership, the author
    gets a 404 reading back the row they just wrote — write allowed, read denied,
    same user, same object.

    Discovery stays membership-scoped: the agent tree must still not list agents
    they never joined. Only reachability BY ID is granted here.
    """
    world = group_world
    org_id, admin = world["org_id"], world["admin"]

    role = create_role(name="org-knowledge-admin", permissions=["manage_instructions"],
                       user_token=admin["token"], org_id=org_id)
    assert role.status_code == 200, role.json()
    gov = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    asg = assign_role(role_id=role.json()["id"], principal_type="user",
                      principal_id=gov["user_id"], user_token=admin["token"], org_id=org_id)
    assert asg.status_code in (200, 201), asg.text

    created = test_client.post(
        "/api/instructions",
        json=_instruction_body("Cite the source table.", [world["agent1"]["id"]]),
        headers=_hdr(gov["token"], org_id),
    )
    assert created.status_code == 200, created.text
    iid = created.json()["id"]

    read_back = test_client.get(f"/api/instructions/{iid}", headers=_hdr(gov["token"], org_id))
    assert read_back.status_code == 200, (
        "org-tier instruction admin could not read back their own write: " + read_back.text
    )

    upd = test_client.put(f"/api/instructions/{iid}", json={"load_mode": "intelligent"},
                          headers=_hdr(gov["token"], org_id))
    assert upd.status_code == 200, upd.text
    after = test_client.get(f"/api/instructions/{iid}", headers=_hdr(gov["token"], org_id))
    assert after.status_code == 200, after.text
    assert after.json()["load_mode"] == "intelligent"

    # Discovery is still membership-scoped — this is reachability, not browsing.
    agents = test_client.get("/api/data_sources", headers=_hdr(gov["token"], org_id))
    assert agents.status_code == 200, agents.text
    assert agents.json() == [], "granting read-by-id must not widen agent discovery"

    # And someone with no authority at all still cannot reach it.
    nobody = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    denied = test_client.get(f"/api/instructions/{iid}", headers=_hdr(nobody["token"], org_id))
    assert denied.status_code == 404, denied.text


@pytest.mark.e2e
def test_clearing_agent_scope_needs_org_authority(test_client, group_world):
    """Emptying an instruction's agent list is 'make this global'.

    An instruction attached to no agent applies to every agent in the org, so
    /instructions/global requires org-level manage_instructions. The update
    path guarded the NEW scope with a bare truthiness check, and an empty list
    is falsy in Python — so a per-agent manager could create a rule scoped to
    their own agent and then publish it org-wide simply by removing that agent.
    The front door was locked and the side door was not.
    """
    world = group_world
    org_id, m1 = world["org_id"], world["m1"]

    # Front door: creating a global directly is refused.
    direct = test_client.post(
        "/api/instructions/global",
        json=_instruction_body("global by an agent manager", []),
        headers=_hdr(m1["token"], org_id),
    )
    assert direct.status_code == 403, direct.text

    created = test_client.post(
        "/api/instructions",
        json=_instruction_body("scoped to my own agent", [world["agent1"]["id"]]),
        headers=_hdr(m1["token"], org_id),
    )
    assert created.status_code == 200, created.text
    iid = created.json()["id"]

    # Side door: same outcome, so it must be refused the same way.
    cleared = test_client.put(
        f"/api/instructions/{iid}",
        json={"data_source_ids": []},
        headers=_hdr(m1["token"], org_id),
    )
    assert cleared.status_code == 403, (
        "clearing the agent scope publishes the rule org-wide and must need the "
        "same authority as creating a global: " + cleared.text
    )

    after = test_client.get(f"/api/instructions/{iid}", headers=_hdr(m1["token"], org_id))
    assert after.status_code == 200, after.text
    assert [d["id"] for d in after.json()["data_sources"]] == [world["agent1"]["id"]], \
        "a refused re-scope must leave the instruction attached to its agent"

    # An org-level holder may do it.
    admin_clear = test_client.put(
        f"/api/instructions/{iid}",
        json={"data_source_ids": []},
        headers=_hdr(world["admin"]["token"], org_id),
    )
    assert admin_clear.status_code == 200, admin_clear.text
