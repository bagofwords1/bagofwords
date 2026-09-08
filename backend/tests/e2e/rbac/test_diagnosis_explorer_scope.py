"""Scope enforcement for the diagnosis explorer.

An org admin sees every run. An agent manager (``manage`` on a data source)
sees only runs on reports that draw ONLY on agents they manage — whatever
the query says. A plain member is refused. This must hold on every endpoint:
runs, tool calls, facets.
"""
import uuid
from datetime import datetime, timedelta

import pytest

pytestmark = pytest.mark.e2e

NOW = datetime.utcnow().replace(microsecond=0)


def _iso(dt):
    return dt.isoformat() + "Z"


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}


@pytest.fixture
def world(bootstrap_admin, invite_user_to_org, sqlite_data_source, grant_resource, create_report,
          seed_agent_executions, rollup_agent_executions):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]
    ds_a = sqlite_data_source(name=f"alpha-{uuid.uuid4().hex[:4]}", user_token=admin["token"], org_id=org_id)
    ds_b = sqlite_data_source(name=f"beta-{uuid.uuid4().hex[:4]}", user_token=admin["token"], org_id=org_id)

    manager_b = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    grant_resource(
        resource_type="data_source", resource_id=ds_b["id"],
        principal_type="user", principal_id=manager_b["user_id"],
        permissions=["manage"], user_token=admin["token"], org_id=org_id,
    )
    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])

    r_a = create_report(title="Alpha only", user_token=admin["token"], org_id=org_id, data_sources=[ds_a["id"]])
    r_b = create_report(title="Beta only", user_token=admin["token"], org_id=org_id, data_sources=[ds_b["id"]])
    r_ab = create_report(title="Alpha and beta", user_token=admin["token"], org_id=org_id, data_sources=[ds_a["id"], ds_b["id"]])
    r_none = create_report(title="No agent", user_token=admin["token"], org_id=org_id)

    def run(prompt, **kw):
        base = dict(user_id=admin["user_id"], prompt=prompt, created_at=NOW - timedelta(days=1), status="error",
                    tools=[{"name": "create_data", "status": "error", "error": "boom"}])
        base.update(kw)
        return base

    ids = {
        "a": seed_agent_executions(org_id, r_a["id"], [run("alpha secret revenue")]),
        "b": seed_agent_executions(org_id, r_b["id"], [run("beta revenue"), run("beta churn", status="success", tools=[])]),
        "ab": seed_agent_executions(org_id, r_ab["id"], [run("joint revenue")]),
        "none": seed_agent_executions(org_id, r_none["id"], [run("orphan revenue")]),
    }
    rollup_agent_executions()
    return {"org_id": org_id, "admin": admin, "manager_b": manager_b, "member": member,
            "ds_a": ds_a, "ds_b": ds_b, "ids": ids}


def _runs(test_client, world, token, q="", **params):
    return test_client.get(
        "/api/console/diagnosis/runs",
        params={"q": q, "start": _iso(NOW - timedelta(days=30)), "end": _iso(NOW + timedelta(days=1)), **params},
        headers=_headers(token, world["org_id"]),
    )


def _ids(resp):
    assert resp.status_code == 200, resp.json()
    return {i["id"] for i in resp.json()["items"]}


def test_admin_sees_every_run(test_client, world):
    all_ids = {i for group in world["ids"].values() for i in group}
    assert _ids(_runs(test_client, world, world["admin"]["token"])) == all_ids


@pytest.mark.parametrize("q", [
    "", "revenue", "status:error", "tool:create_data tool.status:error", "alpha", "secret",
    "report:*", "has:agent", "NOT status:success", "created:-7d", "tools:>=0",
])
def test_agent_manager_sees_only_runs_on_reports_drawing_solely_on_their_agents(test_client, world, q):
    visible = _ids(_runs(test_client, world, world["manager_b"]["token"], q))
    assert visible <= set(world["ids"]["b"]), q
    # ...and nothing from alpha, the joint report, or the agent-less report
    for group in ("a", "ab", "none"):
        assert not (visible & set(world["ids"][group])), (q, group)


def test_agent_manager_gets_their_own_runs_for_a_matching_query(test_client, world):
    resp = _runs(test_client, world, world["manager_b"]["token"], "revenue")
    assert _ids(resp) == {world["ids"]["b"][0]}
    body = resp.json()
    assert body["total"] == 1 and body["total_in_range"] == 2
    assert body["summary"]["matched"] == 1


def test_agent_filter_cannot_widen_a_managers_scope(test_client, world):
    alpha = world["ds_a"]["name"]
    resp = _runs(test_client, world, world["manager_b"]["token"], f'agent:"{alpha}"')
    assert _ids(resp) == set()
    assert _ids(_runs(test_client, world, world["admin"]["token"], f'agent:"{alpha}"')) == set(world["ids"]["a"]) | set(world["ids"]["ab"])


def test_tool_calls_outside_scope_are_absent(test_client, world):
    ids = [world["ids"]["a"][0], world["ids"]["b"][0], world["ids"]["ab"][0]]
    resp = test_client.get(
        "/api/console/diagnosis/runs/tool_calls",
        params={"run_ids": ",".join(ids)},
        headers=_headers(world["manager_b"]["token"], world["org_id"]),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body[world["ids"]["b"][0]]) == 1
    assert body[world["ids"]["a"][0]] == [] and body[world["ids"]["ab"][0]] == []
    admin_body = test_client.get(
        "/api/console/diagnosis/runs/tool_calls",
        params={"run_ids": ",".join(ids)},
        headers=_headers(world["admin"]["token"], world["org_id"]),
    ).json()
    assert all(len(admin_body[i]) == 1 for i in ids)


def test_facets_are_scoped_too(test_client, world):
    def facets(token, field):
        resp = test_client.get(
            f"/api/console/diagnosis/facets/{field}",
            params={"q": "", "start": _iso(NOW - timedelta(days=30)), "end": _iso(NOW + timedelta(days=1))},
            headers=_headers(token, world["org_id"]),
        )
        assert resp.status_code == 200, resp.json()
        return {f["value"]: f["count"] for f in resp.json()}

    assert facets(world["manager_b"]["token"], "status") == {"error": 1, "success": 1}
    assert facets(world["admin"]["token"], "status") == {"error": 4, "success": 1}
    assert facets(world["manager_b"]["token"], "agent") == {world["ds_b"]["name"]: 2}
    assert set(facets(world["admin"]["token"], "agent")) == {world["ds_a"]["name"], world["ds_b"]["name"]}


def test_member_without_any_manage_grant_is_refused(test_client, world):
    token = world["member"]["token"]
    assert _runs(test_client, world, token).status_code == 403
    for path in ("/api/console/diagnosis/fields", "/api/console/diagnosis/facets/status"):
        resp = test_client.get(
            path,
            params={"start": _iso(NOW - timedelta(days=30)), "end": _iso(NOW)},
            headers=_headers(token, world["org_id"]),
        )
        assert resp.status_code == 403, path
