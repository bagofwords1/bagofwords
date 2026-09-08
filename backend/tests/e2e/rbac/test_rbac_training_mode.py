"""
RBAC end-to-end coverage for **training mode** availability and the
**per-agent scoping** of everything a training session produces.

Contract (see ``docs/feedback-loops/training-mode-agent-admins.md``):

  Entry gate (per-agent, not org-wide):
    1. An agent admin (`manage` on the agent) CAN enter training mode on it.
    2. A plain member (`view` only) CANNOT.
    3. Cross-agent isolation — a user who is a MEMBER of agent1 and an ADMIN of
       agent2 is DENIED training on agent1 but allowed on agent2. Holding
       `manage` on some agent must not unlock training on an agent they only view.
    4. A full admin can train any agent (bypass preserved).
    5. When enable_training_mode is disabled, nobody can enter training mode.

  Write scoping (nothing created leaks org-wide):
    6. Instructions — an agent admin cannot author a global instruction, nor one
       on an agent they don't manage.
    7. Evals — an agent admin cannot author a global (data-source-less) eval;
       scoped cases require manage_evals on every referenced agent.

World:
    admin   — full_admin_access (bootstrap owner)
    u       — MEMBER (view) of agent1, ADMIN (manage) of agent2   ← the key case
"""
import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _instruction_body(text, ds_ids):
    return {"text": text, "status": "draft", "category": "general", "data_source_ids": ds_ids}


@pytest.fixture
def training_world(
    test_client,
    bootstrap_admin,
    invite_user_to_org,
    grant_resource,
    sqlite_data_source,
):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]

    agent1 = sqlite_data_source(name="tm_agent1", user_token=admin["token"], org_id=org_id)
    agent2 = sqlite_data_source(name="tm_agent2", user_token=admin["token"], org_id=org_id)

    # u = MEMBER (view) of agent1, ADMIN (manage) of agent2  ← the cross-agent case
    u = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    for ds, perms in ((agent1, ["view"]), (agent2, ["manage"])):
        r = grant_resource(
            resource_type="data_source",
            resource_id=ds["id"],
            principal_type="user",
            principal_id=u["user_id"],
            permissions=perms,
            user_token=admin["token"],
            org_id=org_id,
        )
        assert r.status_code == 200, r.text

    return {"org_id": org_id, "admin": admin, "u": u, "agent1": agent1, "agent2": agent2}


def _report_on(test_client, token, org_id, ds_id):
    r = test_client.post(
        "/api/reports",
        json={"title": "training session", "data_sources": [ds_id]},
        headers=_hdr(token, org_id),
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _set_mode(test_client, token, org_id, report_id, mode):
    return test_client.put(
        f"/api/reports/{report_id}",
        json={"mode": mode},
        headers=_hdr(token, org_id),
    )


# ── Entry gate ───────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_agent_admin_can_enter_training_on_own_agent(test_client, training_world):
    """Story 1: an agent admin (manage grant) can switch a report on that agent
    into training mode, without holding full_admin_access."""
    org = training_world["org_id"]
    u = training_world["u"]
    rid = _report_on(test_client, u["token"], org, training_world["agent2"]["id"])
    resp = _set_mode(test_client, u["token"], org, rid, "training")
    assert resp.status_code == 200, resp.text
    assert resp.json()["mode"] == "training"


@pytest.mark.e2e
def test_member_cannot_enter_training(test_client, training_world):
    """Story 2: a plain member (view only) is denied training on that agent —
    but chat mode still works, so the block is specific to training."""
    org = training_world["org_id"]
    u = training_world["u"]
    rid = _report_on(test_client, u["token"], org, training_world["agent1"]["id"])

    denied = _set_mode(test_client, u["token"], org, rid, "training")
    assert denied.status_code == 403, denied.text

    ok = _set_mode(test_client, u["token"], org, rid, "chat")
    assert ok.status_code == 200, ok.text


@pytest.mark.e2e
def test_member_of_one_agent_admin_of_another_cannot_train_member_agent(test_client, training_world):
    """Story 3 (THE case): the same user is a member of agent1 and an admin of
    agent2. Training is DENIED on agent1 and ALLOWED on agent2 — `manage` on
    agent2 must not leak into agent1."""
    org = training_world["org_id"]
    u = training_world["u"]

    r1 = _report_on(test_client, u["token"], org, training_world["agent1"]["id"])  # member
    r2 = _report_on(test_client, u["token"], org, training_world["agent2"]["id"])  # admin

    assert _set_mode(test_client, u["token"], org, r1, "training").status_code == 403
    assert _set_mode(test_client, u["token"], org, r2, "training").status_code == 200


@pytest.mark.e2e
def test_full_admin_can_train_any_agent(test_client, training_world):
    """Story 4 (backward compat): full admins can still train any agent."""
    org = training_world["org_id"]
    admin = training_world["admin"]
    for agent in ("agent1", "agent2"):
        rid = _report_on(test_client, admin["token"], org, training_world[agent]["id"])
        resp = _set_mode(test_client, admin["token"], org, rid, "training")
        assert resp.status_code == 200, f"{agent}: {resp.text}"


@pytest.mark.e2e
def test_training_blocked_when_org_flag_disabled(test_client, training_world, update_organization_settings):
    """Story 5: with enable_training_mode disabled, NOBODY can enter training —
    not the agent admin, not the full admin. The org flag still hard-blocks."""
    org = training_world["org_id"]
    admin = training_world["admin"]
    u = training_world["u"]

    # Fixture asserts 200 internally and returns the settings dict.
    update_organization_settings(
        config={"enable_training_mode": {"value": False}},
        user_token=admin["token"],
        org_id=org,
    )

    # Agent admin on their own agent → blocked (400, flag off).
    rid_u = _report_on(test_client, u["token"], org, training_world["agent2"]["id"])
    assert _set_mode(test_client, u["token"], org, rid_u, "training").status_code == 400

    # Even a full admin → blocked.
    rid_a = _report_on(test_client, admin["token"], org, training_world["agent2"]["id"])
    assert _set_mode(test_client, admin["token"], org, rid_a, "training").status_code == 400


@pytest.mark.e2e
def test_agent_creator_can_train_agent_they_created(
    test_client, bootstrap_admin, invite_user_to_org, create_role, assign_role, sqlite_data_source,
):
    """A user with `create_data_source` who CREATES an agent becomes its owner
    (a per-DS `manage` grant, which implies manage_instructions) and can enter
    training mode on it — but NOT on an agent they neither created nor manage.

    'If I can create/manage agents I should see training' → yes, for the agents
    you actually own/manage; creating agents does not unlock training on
    someone else's agent."""
    admin = bootstrap_admin("admin")
    org = admin["org_id"]

    # A role whose only power is creating agents, assigned directly to `creator`.
    role = create_role(name="agent-creators", permissions=["create_data_source"],
                       user_token=admin["token"], org_id=org)
    assert role.status_code == 200, role.text
    creator = invite_user_to_org(org_id=org, admin_token=admin["token"])
    asg = assign_role(role_id=role.json()["id"], principal_type="user",
                      principal_id=creator["user_id"], user_token=admin["token"], org_id=org)
    assert asg.status_code in (200, 201), asg.text

    # Admin owns an agent the creator has nothing to do with.
    admin_agent = sqlite_data_source(name="admins_agent", user_token=admin["token"], org_id=org)
    # Creator makes their own agent → becomes its owner (manage grant).
    own_agent = sqlite_data_source(name="creators_agent", user_token=creator["token"], org_id=org)

    # Train the agent they created → allowed.
    rid_own = _report_on(test_client, creator["token"], org, own_agent["id"])
    assert _set_mode(test_client, creator["token"], org, rid_own, "training").status_code == 200

    # They can't even attach the admin's private agent to a report, so training
    # on it is unreachable — create_data_source does not grant cross-agent train.
    rep = test_client.post(
        "/api/reports",
        json={"title": "x", "data_sources": [admin_agent["id"]]},
        headers=_hdr(creator["token"], org),
    )
    assert rep.status_code in (200, 201), rep.text
    # The forbidden agent is dropped from the report (no access), so it has no
    # trainable agent → training is denied.
    assert _set_mode(test_client, creator["token"], org, rep.json()["id"], "training").status_code == 403


# ── Write scoping (HTTP routes) ──────────────────────────────────────────────

@pytest.mark.e2e
def test_agent_admin_instruction_writes_are_scoped(test_client, training_world):
    """Story 6: an agent admin's instruction writes stay on their agent — a
    global (data-source-less) instruction and one on an agent they only view
    are both denied."""
    org = training_world["org_id"]
    u = training_world["u"]
    agent1 = training_world["agent1"]["id"]
    agent2 = training_world["agent2"]["id"]

    # On the agent they manage → allowed.
    ok = test_client.post(
        "/api/instructions",
        json=_instruction_body("scoped to agent2", [agent2]),
        headers=_hdr(u["token"], org),
    )
    assert ok.status_code == 200, ok.text

    # Global (no data source) → org-level only → 403.
    glob = test_client.post(
        "/api/instructions/global",
        json=_instruction_body("tries global", []),
        headers=_hdr(u["token"], org),
    )
    assert glob.status_code == 403, glob.text

    # On an agent they only view → 403.
    cross = test_client.post(
        "/api/instructions",
        json=_instruction_body("tries agent1", [agent1]),
        headers=_hdr(u["token"], org),
    )
    assert cross.status_code == 403, cross.text


@pytest.mark.e2e
def test_agent_admin_eval_writes_are_scoped(test_client, training_world):
    """Story 7: an agent admin can create an eval case scoped to their agent,
    but NOT a global (empty data_source_ids_json) case — that stays org-level.
    This is the create_case bypass that is now closed."""
    org = training_world["org_id"]
    admin = training_world["admin"]
    u = training_world["u"]
    agent1 = training_world["agent1"]["id"]
    agent2 = training_world["agent2"]["id"]

    # Admin owns the suite (suite creation is org-level manage_evals).
    suite = test_client.post(
        "/api/tests/suites",
        json={"name": "TM Eval Suite", "description": None},
        headers=_hdr(admin["token"], org),
    )
    assert suite.status_code == 200, suite.text
    suite_id = suite.json()["id"]

    def _case(token, ds_ids):
        return test_client.post(
            f"/api/tests/suites/{suite_id}/cases",
            json={
                "name": f"case-{ds_ids}",
                "prompt_json": {"content": "evaluate"},
                "expectations_json": {"spec_version": 1, "rules": [], "order_mode": "flexible"},
                "data_source_ids_json": ds_ids,
            },
            headers=_hdr(token, org),
        )

    # Scoped to the agent they manage → allowed.
    assert _case(u["token"], [agent2]).status_code == 200
    # Global (empty) → 403 (the closed bypass).
    assert _case(u["token"], []).status_code == 403
    # Scoped to an agent they only view → 403.
    assert _case(u["token"], [agent1]).status_code == 403
    # Control: full admin can still create a global eval.
    assert _case(admin["token"], []).status_code == 200


# ── Entry gate on CREATE (home prompt box picks Training before a report exists) ──
#
# The home page creates the report and hands off to it; the mode picked there
# must land on the report row itself (otherwise the report opens as chat and
# every message after the first runs as chat). Same gate as the PUT path.

def _create_report(test_client, token, org_id, ds_ids, mode=None):
    body = {"title": "from home", "data_sources": ds_ids}
    if mode is not None:
        body["mode"] = mode
    return test_client.post("/api/reports", json=body, headers=_hdr(token, org_id))


def _get_report(test_client, token, org_id, report_id):
    r = test_client.get(f"/api/reports/{report_id}", headers=_hdr(token, org_id))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.e2e
def test_create_report_defaults_to_chat_mode(test_client, training_world):
    """Omitting mode on create yields a chat report (unchanged default)."""
    org = training_world["org_id"]
    u = training_world["u"]
    r = _create_report(test_client, u["token"], org, [training_world["agent1"]["id"]])
    assert r.status_code in (200, 201), r.text
    assert r.json()["mode"] == "chat"
    assert _get_report(test_client, u["token"], org, r.json()["id"])["mode"] == "chat"


@pytest.mark.e2e
def test_agent_admin_can_create_report_in_training_mode(test_client, training_world):
    """An agent admin who picks Training on the home prompt box gets a report
    that IS in training mode — on the create response and when re-fetched."""
    org = training_world["org_id"]
    u = training_world["u"]
    r = _create_report(test_client, u["token"], org, [training_world["agent2"]["id"]], mode="training")
    assert r.status_code in (200, 201), r.text
    assert r.json()["mode"] == "training"
    assert _get_report(test_client, u["token"], org, r.json()["id"])["mode"] == "training"


@pytest.mark.e2e
def test_member_cannot_create_report_in_training_mode(test_client, training_world):
    """A plain member is denied a training report on that agent (403), and no
    report is left behind — but a chat report on the same agent still works."""
    org = training_world["org_id"]
    u = training_world["u"]
    before = test_client.get("/api/reports", headers=_hdr(u["token"], org))
    assert before.status_code == 200, before.text
    before_total = before.json().get("total", len(before.json().get("items", [])))

    denied = _create_report(test_client, u["token"], org, [training_world["agent1"]["id"]], mode="training")
    assert denied.status_code == 403, denied.text

    after = test_client.get("/api/reports", headers=_hdr(u["token"], org))
    after_total = after.json().get("total", len(after.json().get("items", [])))
    assert after_total == before_total

    ok = _create_report(test_client, u["token"], org, [training_world["agent1"]["id"]], mode="chat")
    assert ok.status_code in (200, 201), ok.text
    assert ok.json()["mode"] == "chat"


@pytest.mark.e2e
def test_create_training_report_requires_manage_on_every_agent(test_client, training_world):
    """Cross-agent: a user who manages agent2 but only views agent1 can create a
    training report on agent2, not on agent1, and not on both together."""
    org = training_world["org_id"]
    u = training_world["u"]
    a1, a2 = training_world["agent1"]["id"], training_world["agent2"]["id"]

    assert _create_report(test_client, u["token"], org, [a2], mode="training").status_code in (200, 201)
    assert _create_report(test_client, u["token"], org, [a1], mode="training").status_code == 403
    assert _create_report(test_client, u["token"], org, [a1, a2], mode="training").status_code == 403


@pytest.mark.e2e
def test_full_admin_can_create_training_report_on_any_agent(test_client, training_world):
    org = training_world["org_id"]
    admin = training_world["admin"]
    for agent in ("agent1", "agent2"):
        r = _create_report(test_client, admin["token"], org, [training_world[agent]["id"]], mode="training")
        assert r.status_code in (200, 201), f"{agent}: {r.text}"
        assert r.json()["mode"] == "training"


@pytest.mark.e2e
def test_create_training_report_blocked_when_org_flag_disabled(
    test_client, training_world, update_organization_settings
):
    """With enable_training_mode off, nobody can create a training report —
    not the agent admin, not the full admin."""
    org = training_world["org_id"]
    admin = training_world["admin"]
    u = training_world["u"]
    update_organization_settings(
        config={"enable_training_mode": {"value": False}},
        user_token=admin["token"],
        org_id=org,
    )
    a2 = training_world["agent2"]["id"]
    assert _create_report(test_client, u["token"], org, [a2], mode="training").status_code == 400
    assert _create_report(test_client, admin["token"], org, [a2], mode="training").status_code == 400
    # Chat creation is unaffected by the flag.
    assert _create_report(test_client, u["token"], org, [a2], mode="chat").status_code in (200, 201)
