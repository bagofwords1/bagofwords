"""The monitoring console is open to agent managers, scoped to their agents.

Historically every ``/console/*`` endpoint required org-level ``manage_settings``
(admins only). It now admits anyone holding ``manage`` on at least one agent —
but they see only that agent's activity, never the org's.

Cast:
    admin    — org admin, org-wide console
    manager  — plain member with a `manage` grant on agent_a only
    outsider — plain member with no grants at all
"""
import uuid
from datetime import datetime

import pytest


def _headers(token: str, org_id: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def console_world(
    test_client,
    bootstrap_admin,
    invite_user_to_org,
    sqlite_data_source,
    grant_resource,
    create_report,
    seed_agent_executions,
):
    """Two private agents, one report + one seeded run each, and three actors."""
    admin = bootstrap_admin("consoleadmin")
    org_id = admin["org_id"]

    agent_a = sqlite_data_source(name=f"agent_a_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=org_id)
    agent_b = sqlite_data_source(name=f"agent_b_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=org_id)

    manager = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    outsider = invite_user_to_org(org_id=org_id, admin_token=admin["token"])

    grant = grant_resource(
        resource_type="data_source", resource_id=agent_a["id"],
        principal_type="user", principal_id=manager["user_id"],
        permissions=["manage"], user_token=admin["token"], org_id=org_id,
    )
    assert grant.status_code in (200, 201), grant.json()

    report_a = create_report(title="Report A", user_token=admin["token"], org_id=org_id,
                             data_sources=[agent_a["id"]])
    report_b = create_report(title="Report B", user_token=admin["token"], org_id=org_id,
                             data_sources=[agent_b["id"]])

    now = datetime.utcnow()
    seed_agent_executions(org_id, report_a["id"], [
        {"user_id": admin["user_id"], "prompt": "runs on agent a", "created_at": now},
    ])
    seed_agent_executions(org_id, report_b["id"], [
        {"user_id": admin["user_id"], "prompt": "runs on agent b", "created_at": now},
    ])

    return {
        "org_id": org_id, "admin": admin, "manager": manager, "outsider": outsider,
        "agent_a": agent_a, "agent_b": agent_b, "report_a": report_a, "report_b": report_b,
    }


# Endpoints every console user hits; all must agree on who gets in.
CONSOLE_ENDPOINTS = [
    "/api/console/metrics",
    "/api/console/metrics/timeseries",
    "/api/console/metrics/table-usage",
    "/api/console/metrics/llm-usage",
    "/api/console/metrics/top-users",
    "/api/console/agent_executions/summaries",
    "/api/console/diagnosis/metrics",
    "/api/console/diagnosis/timeseries",
    "/api/console/diagnosis/users",
]


@pytest.mark.e2e
@pytest.mark.parametrize("endpoint", CONSOLE_ENDPOINTS)
def test_console_open_to_agent_managers_closed_to_plain_members(console_world, test_client, endpoint):
    w = console_world
    for actor in ("admin", "manager"):
        resp = test_client.get(endpoint, headers=_headers(w[actor]["token"], w["org_id"]))
        assert resp.status_code == 200, f"{actor} denied on {endpoint}: {resp.text}"

    denied = test_client.get(endpoint, headers=_headers(w["outsider"]["token"], w["org_id"]))
    assert denied.status_code == 403, f"outsider allowed on {endpoint}: {resp.text}"


@pytest.mark.e2e
def test_manager_only_sees_runs_of_agents_they_manage(console_world, get_agent_execution_summaries):
    w = console_world

    admin_view = get_agent_execution_summaries(user_token=w["admin"]["token"], org_id=w["org_id"])
    assert admin_view.status_code == 200
    prompts = {i["prompt"] for i in admin_view.json()["items"]}
    assert prompts == {"runs on agent a", "runs on agent b"}

    manager_view = get_agent_execution_summaries(user_token=w["manager"]["token"], org_id=w["org_id"])
    assert manager_view.status_code == 200
    assert [i["prompt"] for i in manager_view.json()["items"]] == ["runs on agent a"]
    assert manager_view.json()["total_items"] == 1


@pytest.mark.e2e
def test_manager_agent_filter_narrows_but_never_widens(console_world, get_agent_execution_summaries):
    w = console_world

    # Selecting the agent they manage is a no-op narrowing.
    own = get_agent_execution_summaries(
        user_token=w["manager"]["token"], org_id=w["org_id"], data_source_ids=w["agent_a"]["id"],
    )
    assert own.status_code == 200
    assert [i["prompt"] for i in own.json()["items"]] == ["runs on agent a"]

    # Asking for an agent they don't manage is refused rather than silently
    # widened to "everything" (an empty filter would read as no filter).
    foreign = get_agent_execution_summaries(
        user_token=w["manager"]["token"], org_id=w["org_id"], data_source_ids=w["agent_b"]["id"],
    )
    assert foreign.status_code == 403, foreign.text

    # A mixed request keeps only the in-scope half.
    mixed = get_agent_execution_summaries(
        user_token=w["manager"]["token"], org_id=w["org_id"],
        data_source_ids=f"{w['agent_a']['id']},{w['agent_b']['id']}",
    )
    assert mixed.status_code == 200
    assert [i["prompt"] for i in mixed.json()["items"]] == ["runs on agent a"]

    # The admin's own filter still works unchanged.
    admin_b = get_agent_execution_summaries(
        user_token=w["admin"]["token"], org_id=w["org_id"], data_source_ids=w["agent_b"]["id"],
    )
    assert admin_b.status_code == 200
    assert [i["prompt"] for i in admin_b.json()["items"]] == ["runs on agent b"]


@pytest.mark.e2e
def test_trace_drilldown_follows_the_same_scope(console_world, test_client):
    """A manager can replay a conversation on their agent, not on someone else's."""
    w = console_world

    own = test_client.get(
        f"/api/console/reports/{w['report_a']['id']}/conversation",
        headers=_headers(w["manager"]["token"], w["org_id"]),
    )
    assert own.status_code == 200, own.text

    foreign = test_client.get(
        f"/api/console/reports/{w['report_b']['id']}/conversation",
        headers=_headers(w["manager"]["token"], w["org_id"]),
    )
    assert foreign.status_code == 403, foreign.text

    # Admins keep the org-wide drill-down.
    admin_foreign = test_client.get(
        f"/api/console/reports/{w['report_b']['id']}/conversation",
        headers=_headers(w["admin"]["token"], w["org_id"]),
    )
    assert admin_foreign.status_code == 200, admin_foreign.text


@pytest.mark.e2e
def test_cost_dashboard_follows_the_same_gate(console_world, test_client, enterprise_license):
    """The enterprise cost tab admits agent managers too, still scoped."""
    w = console_world

    for actor in ("admin", "manager"):
        resp = test_client.get(
            "/api/console/metrics/cost", headers=_headers(w[actor]["token"], w["org_id"]),
        )
        assert resp.status_code == 200, f"{actor} denied on cost: {resp.text}"

    denied = test_client.get(
        "/api/console/metrics/cost", headers=_headers(w["outsider"]["token"], w["org_id"]),
    )
    assert denied.status_code == 403, denied.text

    foreign = test_client.get(
        "/api/console/metrics/cost",
        headers=_headers(w["manager"]["token"], w["org_id"]),
        params={"data_source_ids": w["agent_b"]["id"]},
    )
    assert foreign.status_code == 403, foreign.text


@pytest.mark.e2e
def test_manage_grant_via_group_also_opens_the_console(
    console_world, test_client, enterprise_license, create_group, add_user_to_group, grant_resource,
    get_agent_execution_summaries,
):
    """The gate reads resolved permissions, so a group-held grant counts too."""
    w = console_world
    admin_token, org_id = w["admin"]["token"], w["org_id"]

    group = create_group(name=f"analysts_{uuid.uuid4().hex[:6]}", user_token=admin_token, org_id=org_id)
    assert group.status_code in (200, 201), group.json()
    group_id = group.json()["id"]

    added = add_user_to_group(group_id=group_id, user_id=w["outsider"]["user_id"],
                              user_token=admin_token, org_id=org_id)
    assert added.status_code in (200, 201), added.json()

    # Before the grant the outsider is still locked out.
    assert get_agent_execution_summaries(
        user_token=w["outsider"]["token"], org_id=org_id
    ).status_code == 403

    granted = grant_resource(
        resource_type="data_source", resource_id=w["agent_b"]["id"],
        principal_type="group", principal_id=group_id,
        permissions=["manage"], user_token=admin_token, org_id=org_id,
    )
    assert granted.status_code in (200, 201), granted.json()

    resp = get_agent_execution_summaries(user_token=w["outsider"]["token"], org_id=org_id)
    assert resp.status_code == 200, resp.text
    assert [i["prompt"] for i in resp.json()["items"]] == ["runs on agent b"]
