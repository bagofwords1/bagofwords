"""The eval AGENT TOOLS resolve authority the same way the HTTP routes do.

They did not. Five tools (`search_evals`, `get_eval_run`, `get_eval_runs`,
`run_eval`, `stop_eval_run`) tested ``has_org_permission("manage_evals")`` alone
and denied everyone else — while the tool catalog, which DOES resolve per-agent
grants, still advertised them. An agent owner in training mode was therefore
offered eval tools that could only fail: "Searched evals → No matching evals",
then an unexplained platform error, on an agent whose Evals panel listed cases
perfectly well.

Both sides now resolve through ``app.core.eval_scope``, so a tool cannot be
stricter (deny a legitimate manager) or looser (list another agent's cases) than
the route serving the same data.
"""
import asyncio
import uuid

import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _run(coro):
    return asyncio.run(coro)


async def _run_tool(tool, tool_input: dict, user_id: str, org_id: str, report_id=None):
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User

    async with async_session_maker() as db:
        user = await db.get(User, str(user_id))
        organization = await db.get(Organization, str(org_id))
        report = None
        if report_id:
            report = (await db.execute(
                select(Report).options(selectinload(Report.data_sources))
                .where(Report.id == str(report_id))
            )).scalar_one()
        runtime_ctx = {"db": db, "user": user, "organization": organization, "report": report}
        return [evt async for evt in tool.run_stream(tool_input, runtime_ctx)]


def _payload(events):
    ends = [e for e in events if e.type == "tool.end"]
    errs = [e for e in events if e.type == "tool.error"]
    assert not errs, f"tool errored: {[e.payload for e in errs]}"
    assert ends, f"no tool.end in {[e.type for e in events]}"
    return ends[-1].payload


@pytest.fixture
def tool_world(
    test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source, grant_resource
):
    admin = bootstrap_admin("tooladmin")
    org_id = admin["org_id"]
    ds_a = sqlite_data_source(name=f"t_a_{uuid.uuid4().hex[:4]}", user_token=admin["token"], org_id=org_id)
    ds_b = sqlite_data_source(name=f"t_b_{uuid.uuid4().hex[:4]}", user_token=admin["token"], org_id=org_id)

    mgr_a = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    grant_resource(
        resource_type="data_source", resource_id=ds_a["id"],
        principal_type="user", principal_id=mgr_a["user_id"],
        permissions=["manage_evals"], user_token=admin["token"], org_id=org_id,
    )

    suite_id = test_client.post(
        "/api/tests/suites", json={"name": f"ts_{uuid.uuid4().hex[:6]}"},
        headers=_hdr(admin["token"], org_id),
    ).json()["id"]

    def _mk(ds_ids, name):
        return test_client.post(
            f"/api/tests/suites/{suite_id}/cases",
            json={
                "name": name,
                "prompt_json": {"content": f"q {name}"},
                "expectations_json": {"rules": []},
                "data_source_ids_json": ds_ids,
            },
            headers=_hdr(admin["token"], org_id),
        ).json()["id"]

    return {
        "org_id": org_id, "admin": admin, "mgr_a": mgr_a,
        "ds_a": ds_a, "ds_b": ds_b, "suite_id": suite_id,
        "case_a": _mk([ds_a["id"]], f"a_{uuid.uuid4().hex[:4]}"),
        "case_b": _mk([ds_b["id"]], f"b_{uuid.uuid4().hex[:4]}"),
        "case_ab": _mk([ds_a["id"], ds_b["id"]], f"ab_{uuid.uuid4().hex[:4]}"),
        "case_global": _mk([], f"g_{uuid.uuid4().hex[:4]}"),
    }


@pytest.mark.e2e
def test_search_evals_serves_a_per_agent_manager(tool_world):
    """The reported bug: an agent manager asking "what evals do you have" got a
    PERMISSION_DENIED instead of their own agent's cases."""
    from app.ai.tools.implementations.search_evals import SearchEvalsTool

    w = tool_world
    events = _run(_run_tool(
        SearchEvalsTool(), {"status": "all", "limit": 50},
        w["mgr_a"]["user_id"], w["org_id"],
    ))
    ids = {i["id"] for i in _payload(events)["output"]["items"]}

    # Authority is an intersection: their own agent's case and nothing else. The
    # routing eval also targets B, and an org-wide case covers every agent, so
    # neither is theirs to see.
    assert w["case_a"] in ids
    assert w["case_ab"] not in ids, "a case also targeting an unmanaged agent is not mine"
    assert w["case_global"] not in ids, "an org-wide eval takes org-level manage_evals"
    assert w["case_b"] not in ids, "another agent's case must not leak into the tool"


@pytest.mark.e2e
def test_search_evals_matches_the_http_route_exactly(test_client, tool_world):
    """Tool and route serve the same data to the same person — the drift that
    made one deny while the other answered is what this pins."""
    from app.ai.tools.implementations.search_evals import SearchEvalsTool

    w = tool_world
    events = _run(_run_tool(
        SearchEvalsTool(), {"status": "all", "limit": 50},
        w["mgr_a"]["user_id"], w["org_id"],
    ))
    tool_ids = {i["id"] for i in _payload(events)["output"]["items"]}

    route = test_client.get(
        "/api/tests/cases?limit=500", headers=_hdr(w["mgr_a"]["token"], w["org_id"])
    )
    assert route.status_code == 200, route.text
    route_ids = {c["id"] for c in route.json()}

    assert tool_ids == route_ids, (
        f"tool and route disagree: only-tool={tool_ids - route_ids}, "
        f"only-route={route_ids - tool_ids}"
    )


@pytest.mark.e2e
def test_search_evals_still_denies_someone_with_no_eval_authority(
    tool_world, invite_user_to_org
):
    """Relaxing the gate must not open it to everyone."""
    from app.ai.tools.implementations.search_evals import SearchEvalsTool

    w = tool_world
    nobody = invite_user_to_org(org_id=w["org_id"], admin_token=w["admin"]["token"])
    events = _run(_run_tool(
        SearchEvalsTool(), {"status": "all", "limit": 50},
        nobody["user_id"], w["org_id"],
    ))
    errs = [e for e in events if e.type == "tool.error"]
    assert errs, "a member with no eval grant anywhere must be denied"
    assert errs[-1].payload.get("code") == "PERMISSION_DENIED"


@pytest.mark.e2e
def test_search_evals_is_bounded_to_the_session_agents(test_client, tool_world):
    """A training session pinned to one agent must surface that agent's evals
    only — not every agent the caller happens to manage. Org-wide cases stay
    visible, since they run against the pinned agent too (the same rule the
    standing <instructions> block applies to global instructions)."""
    from app.ai.tools.implementations.search_evals import SearchEvalsTool

    w = tool_world
    # The admin manages both agents, so authority alone would show everything.
    report = test_client.post(
        "/api/reports",
        json={"title": "training", "data_sources": [w["ds_a"]["id"]]},
        headers=_hdr(w["admin"]["token"], w["org_id"]),
    )
    assert report.status_code == 200, report.text

    events = _run(_run_tool(
        SearchEvalsTool(), {"status": "all", "limit": 50},
        w["admin"]["user_id"], w["org_id"], report_id=report.json()["id"],
    ))
    ids = {i["id"] for i in _payload(events)["output"]["items"]}

    # Relevance is a UNION where authority is an intersection: they answer
    # different questions. The admin owns everything, so what is filtered here is
    # only what the session is not about.
    assert w["case_a"] in ids
    assert w["case_ab"] in ids, "a routing eval touching the pinned agent concerns it"
    assert w["case_global"] in ids, "an org-wide eval applies to the pinned agent too"
    assert w["case_b"] not in ids, \
        "an agent outside the session must not appear, even to someone who manages it"


@pytest.mark.e2e
def test_search_evals_unpinned_session_falls_back_to_authority(test_client, tool_world):
    """An Auto session pins nothing, so it imposes no session bound — authority
    alone decides, and that is the whole point of Auto."""
    from app.ai.tools.implementations.search_evals import SearchEvalsTool

    w = tool_world
    report = test_client.post(
        "/api/reports", json={"title": "auto", "data_sources": []},
        headers=_hdr(w["admin"]["token"], w["org_id"]),
    )
    assert report.status_code == 200, report.text

    events = _run(_run_tool(
        SearchEvalsTool(), {"status": "all", "limit": 50},
        w["admin"]["user_id"], w["org_id"], report_id=report.json()["id"],
    ))
    ids = {i["id"] for i in _payload(events)["output"]["items"]}
    assert {w["case_a"], w["case_b"], w["case_ab"], w["case_global"]} <= ids
