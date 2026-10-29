"""MCP tools that take a report, visualization or artifact id must apply the
same access rules as the web app.

Contract:
- Tools that take a ``report_id`` write into that report (tracking
  completions, queries, artifacts), so they act only on reports the caller
  owns in the caller's organization — like the web app's owner-only
  ``create_reports`` routes. Admins included: mutations stay owner-only.
- The app-only read tools (``get_visualization``, ``get_artifact_data``)
  follow the parent report's artifact visibility, scoped to the caller's org.
- A foreign id gets the same answer as a missing one.
"""

import asyncio
import json
import uuid

import pytest


def _call(test_client, api_key, name, arguments):
    response = test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}},
        headers={"X-API-Key": api_key},
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    try:
        payload = json.loads(result["content"][0]["text"])
    except (ValueError, KeyError, IndexError):
        payload = {}
    return result["isError"], payload


def _refused(is_error, payload):
    """A tool refused the id: an MCP error, or a structured failure."""
    if is_error:
        return True
    if not isinstance(payload, dict):
        return False
    return payload.get("success") is False or "error" in payload


def _seed_dashboard(report_id, org_id, user_id):
    """Put a visualization and a dashboard artifact into a report.

    Direct DB write: producing these through the API needs an LLM, which
    e2e tests don't have. Returns (visualization_id, artifact_version_id).
    """
    from app.settings.database import create_async_session_factory
    from app.models.widget import Widget
    from app.models.query import Query
    from app.models.step import Step
    from app.models.visualization import Visualization
    from app.models.artifact import Artifact, ArtifactVersion

    async def run():
        async with create_async_session_factory()() as db:
            widget = Widget(title="w", slug=f"w-{uuid.uuid4().hex}", report_id=report_id)
            db.add(widget)
            await db.flush()
            query = Query(title="q", report_id=report_id, widget_id=widget.id,
                          organization_id=org_id, user_id=user_id)
            db.add(query)
            await db.flush()
            step = Step(title="s", slug=f"s-{uuid.uuid4().hex}", type="table",
                        widget_id=widget.id, query_id=query.id, code="SELECT 1",
                        data={"rows": [{"v": 1}], "columns": [{"field": "v"}]},
                        status="success")
            db.add(step)
            await db.flush()
            query.default_step_id = step.id
            viz = Visualization(title="viz", status="success", report_id=report_id,
                                query_id=query.id, view={})
            db.add(viz)
            await db.flush()
            artifact = Artifact(report_id=report_id, organization_id=org_id,
                                created_by=user_id, mode="page", title="dash")
            db.add(artifact)
            await db.flush()
            version = ArtifactVersion(artifact_id=artifact.id, report_id=report_id,
                                      user_id=user_id, organization_id=org_id, version=1,
                                      content={"code": "<div/>", "visualization_ids": [str(viz.id)]})
            db.add(version)
            await db.commit()
            return str(viz.id), str(version.id)

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(run())
    finally:
        loop.close()


@pytest.fixture
def mcp_world(test_client, create_user, login_user, whoami, create_organization,
              create_api_key, enable_mcp, invite_user_to_org):
    """An org with an admin and a member, plus a user in a second org.
    Each has their own MCP API key."""
    admin = create_user(email=f"admin_{uuid.uuid4().hex[:8]}@test.com")
    admin_token = login_user(admin["email"], admin["password"])
    admin_info = whoami(admin_token)
    org_id = admin_info["organizations"][0]["id"]
    enable_mcp(user_token=admin_token, org_id=org_id)

    member = invite_user_to_org(org_id=org_id, admin_token=admin_token)

    outsider = create_user(email=f"out_{uuid.uuid4().hex[:8]}@test.com")
    outsider_token = login_user(outsider["email"], outsider["password"])
    outsider_org = create_organization(name="Other org", user_token=outsider_token)
    enable_mcp(user_token=outsider_token, org_id=outsider_org)

    return {
        "org_id": org_id,
        "admin": {"token": admin_token, "user_id": admin_info["id"],
                  "key": create_api_key(user_token=admin_token, org_id=org_id)["key"]},
        "member": {"token": member["token"], "user_id": member["user_id"],
                   "key": create_api_key(user_token=member["token"], org_id=org_id)["key"]},
        "outsider": {"key": create_api_key(user_token=outsider_token, org_id=outsider_org)["key"]},
    }


def _new_report(test_client, api_key):
    is_error, payload = _call(test_client, api_key, "create_report", {"title": "private"})
    assert not is_error, payload
    return payload["report_id"]


# (owner, caller): an outsider, a same-org member on the admin's report, and
# an org admin on a member's report (admins may view, never write).
CALLERS = [("admin", "outsider"), ("admin", "member"), ("member", "admin")]


@pytest.mark.e2e
@pytest.mark.parametrize("owner,caller", CALLERS)
def test_report_tools_refuse_reports_the_caller_does_not_own(
    test_client, mcp_world, get_completions, owner, caller,
):
    owner_info = mcp_world[owner]
    report_id = _new_report(test_client, owner_info["key"])
    _, artifact_id = _seed_dashboard(report_id, mcp_world["org_id"], owner_info["user_id"])

    def completion_count():
        return len(get_completions(report_id=report_id, user_token=owner_info["token"],
                                   org_id=mcp_world["org_id"]))

    before = completion_count()
    calls = [
        ("get_context", {"report_id": report_id}),
        ("inspect_data", {"report_id": report_id, "prompt": "peek"}),
        ("create_data", {"report_id": report_id, "prompt": "count rows"}),
        ("list_agent_tools", {"report_id": report_id}),
        ("create_artifact", {"report_id": report_id, "prompt": "dashboard"}),
        ("edit_artifact", {"report_id": report_id, "artifact_id": artifact_id,
                           "edit_instruction": "make it blue"}),
    ]
    caller_key = mcp_world[caller]["key"]
    for name, arguments in calls:
        is_error, payload = _call(test_client, caller_key, name, arguments)
        assert _refused(is_error, payload), f"{name} acted on a report {caller} does not own: {payload}"
        assert payload.get("tools") in (None, []), f"{name} leaked agent tools: {payload}"

    assert completion_count() == before, "a refused caller still wrote into the owner's report"

    # Control: the owner's own call goes through and is recorded — proves the
    # count above would have moved had the guard let a write through.
    is_error, _ = _call(test_client, owner_info["key"], "get_context", {"report_id": report_id})
    assert not is_error
    assert completion_count() > before


@pytest.mark.e2e
def test_edit_artifact_cannot_pull_in_another_reports_artifact(test_client, mcp_world):
    admin, member = mcp_world["admin"], mcp_world["member"]
    victim_report = _new_report(test_client, admin["key"])
    _, victim_artifact = _seed_dashboard(victim_report, mcp_world["org_id"], admin["user_id"])

    own_report = _new_report(test_client, member["key"])
    is_error, payload = _call(test_client, member["key"], "edit_artifact", {
        "report_id": own_report, "artifact_id": victim_artifact, "edit_instruction": "copy it",
    })
    assert _refused(is_error, payload)
    assert "not found" in (payload.get("error_message") or "").lower()


@pytest.mark.e2e
def test_app_tools_follow_report_visibility(test_client, mcp_world, set_visibility):
    admin = mcp_world["admin"]
    report_id = _new_report(test_client, admin["key"])
    viz_id, artifact_id = _seed_dashboard(report_id, mcp_world["org_id"], admin["user_id"])

    def fetch(who):
        key = mcp_world[who]["key"]
        _, viz = _call(test_client, key, "get_visualization", {"visualization_id": viz_id})
        _, art = _call(test_client, key, "get_artifact_data", {"artifact_id": artifact_id})
        return viz, art

    def assert_hidden(who):
        viz, art = fetch(who)
        assert "error" in viz and "data" not in viz, f"{who} read the visualization: {viz}"
        assert "error" in art and "code" not in art, f"{who} read the artifact: {art}"

    def assert_visible(who):
        viz, art = fetch(who)
        assert viz.get("id") == viz_id and viz["data"]["rows"], f"{who} lost access: {viz}"
        assert "error" not in art, f"{who} lost access: {art}"

    assert_visible("admin")
    assert_hidden("outsider")
    assert_hidden("member")  # private report: owner only

    set_visibility(report_id, "artifact", "internal",
                   user_token=admin["token"], org_id=mcp_world["org_id"])
    assert_visible("member")
    assert_hidden("outsider")  # org-internal never reaches another org
