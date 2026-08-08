"""GET /instructions/{id}/resolved-evals — the eval panel behind a staged
instruction change.

The contract worth pinning down is that a suite is a HOME and a case's agents
are its SCOPE (see the ``TestSuite`` model docstring), so the two disagree in
both directions and the panel has to follow the scope:

  * a case filed in agent A's suite but targeting agent B must not appear
    under A
  * an agent-less ("Auto") case filed in an org-wide suite runs against every
    agent and must appear under each of them
"""
import uuid

import pytest


def _panel(test_client, instruction_id, token, org_id, agent_ids=None):
    url = f"/api/instructions/{instruction_id}/resolved-evals"
    if agent_ids:
        url += "?agent_ids=" + ",".join(agent_ids)
    resp = test_client.get(url, headers={
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": str(org_id),
    })
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _group(payload, label):
    for g in payload["groups"]:
        if g["label"] == label:
            return g
    raise AssertionError(f"no group {label!r} in {[g['label'] for g in payload['groups']]}")


def _suite(group, name):
    for s in group["suites"]:
        if s["name"] == name:
            return s
    raise AssertionError(f"no suite {name!r} in {[s['name'] for s in group['suites']]}")


@pytest.fixture
def scenario(create_user, login_user, whoami, create_data_source, create_test_suite, create_test_case):
    """Two agents, three suites, four cases — one of each awkward shape."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    auth = dict(user_token=token, org_id=org_id)

    sales = create_data_source(name="Sales DB", type="network_dir", config={"root_path": "/tmp"},
                               credentials={"auth_type": "none"}, **auth)
    support = create_data_source(name="Support DB", type="network_dir", config={"root_path": "/tmp"},
                                 credentials={"auth_type": "none"}, **auth)

    core = create_test_suite(name="Core checks", data_source_id=sales["id"], **auth)
    drafts = create_test_suite(name="Drafts", data_source_id=sales["id"], **auth)
    shared = create_test_suite(name="Shared checks", **auth)  # org-wide home

    own = create_test_case(suite_id=core["id"], name="revenue by region",
                           data_source_ids_json=[sales["id"]], **auth)
    # Filed in Sales' suite, targets Support.
    crossed = create_test_case(suite_id=core["id"], name="support backlog",
                               data_source_ids_json=[support["id"]], **auth)
    # Targets nobody -> runs against every agent, filed org-wide.
    auto = create_test_case(suite_id=shared["id"], name="never invents columns",
                            data_source_ids_json=[], **auth)

    return {
        "token": token, "org_id": org_id, "auth": auth,
        "sales": sales, "support": support,
        "core": core, "drafts": drafts, "shared": shared,
        "own": own, "crossed": crossed, "auto": auto,
    }


@pytest.mark.e2e
def test_scoped_instruction_lists_suites_for_its_own_agent(test_client, create_instruction, scenario):
    s = scenario
    instruction = create_instruction(text="Group revenue by fiscal quarter.",
                                     data_source_ids=[s["sales"]["id"]], **s["auth"])

    payload = _panel(test_client, instruction["id"], s["token"], s["org_id"])

    assert payload["is_global"] is False
    assert payload["scope_source"] == "instruction"
    assert [g["label"] for g in payload["groups"]] == ["Sales DB"]

    sales = _group(payload, "Sales DB")
    names = {c["name"] for suite in sales["suites"] for c in suite["cases"]}
    # The Auto case runs for this agent even though it is filed org-wide...
    assert "never invents columns" in names
    assert _suite(sales, "Shared checks")["is_home"] is False
    # ...and the case filed here but aimed at another agent does not.
    assert "support backlog" not in names
    assert {c["name"] for c in _suite(sales, "Core checks")["cases"]} == {"revenue by region"}

    # The agent's empty Drafts still shows: it is where the next eval lands.
    drafts = _suite(sales, "Drafts")
    assert drafts["case_count"] == 0
    assert drafts["is_home"] is True and drafts["is_drafts"] is True

    # Suites holding runnable cases sort above the empty shelves.
    assert sales["suites"][-1]["name"] == "Drafts"

    # The flat list is exactly what a run would execute.
    assert payload["case_count"] == 2
    assert {c["name"] for c in payload["cases"]} == {"revenue by region", "never invents columns"}


@pytest.mark.e2e
def test_global_instruction_uses_org_wide_plus_the_chats_agents(test_client, create_instruction, scenario):
    s = scenario
    instruction = create_instruction(text="Never fabricate a column.", data_source_ids=[], **s["auth"])

    # No chat agents: a global instruction owns only the agent-less cases.
    bare = _panel(test_client, instruction["id"], s["token"], s["org_id"])
    assert bare["is_global"] is True and bare["scope_source"] == "none"
    assert [g["label"] for g in bare["groups"]] == ["Org-wide"]
    assert bare["case_count"] == 1

    # With the conversation's agents, they are appended as their own groups.
    scoped = _panel(test_client, instruction["id"], s["token"], s["org_id"],
                    agent_ids=[s["sales"]["id"], s["support"]["id"]])
    assert scoped["scope_source"] == "report"
    assert [g["label"] for g in scoped["groups"]] == ["Org-wide", "Sales DB", "Support DB"]

    # The Auto case is claimed once by Org-wide, not repeated (and re-run) under
    # every agent it also resolves for.
    assert [c["name"] for c in scoped["cases"]].count("never invents columns") == 1
    assert {c["name"] for suite in _group(scoped, "Sales DB")["suites"] for c in suite["cases"]} == {"revenue by region"}
    assert {c["name"] for suite in _group(scoped, "Support DB")["suites"] for c in suite["cases"]} == {"support backlog"}


@pytest.mark.e2e
def test_agent_ids_cannot_widen_what_the_caller_may_see(
    test_client, create_user, login_user, whoami, create_instruction, scenario,
):
    """``agent_ids`` is a request from the client, not authority: a member who
    cannot see an agent must not learn its suite and case names by naming it."""
    s = scenario
    instruction = create_instruction(text="Never fabricate a column.", data_source_ids=[], **s["auth"])

    # A plain member of the SAME org: they can read the global instruction, but
    # the agents are private (is_public defaults to False) and ungranted.
    email = f"member_{uuid.uuid4().hex[:6]}@test.com"
    invite = test_client.post(
        f"/api/organizations/{s['org_id']}/members",
        json={"organization_id": s["org_id"], "email": email, "role": "member"},
        headers={"Authorization": f"Bearer {s['token']}", "X-Organization-Id": str(s["org_id"])},
    )
    assert invite.status_code == 200, invite.json()
    create_user(email=email, password="test123")
    member_token = login_user(email=email, password="test123")

    payload = _panel(test_client, instruction["id"], member_token, s["org_id"],
                     agent_ids=[s["sales"]["id"], s["support"]["id"]])
    assert payload["agent_count"] == 0
    assert [g["label"] for g in payload["groups"]] == ["Org-wide"]
