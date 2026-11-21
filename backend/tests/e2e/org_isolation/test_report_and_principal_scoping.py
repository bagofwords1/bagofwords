"""People, platforms and run records named in report / access requests must
belong to the organization the request acts in.

Every refusal is paired with the same request using an id of the caller's own
organization, so a change that simply breaks the route cannot pass.
"""
import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.agent_execution import AgentExecution
from app.models.completion import Completion
from app.models.external_platform import ExternalPlatform
from app.models.organization import Organization
from app.models.query import Query
from app.models.report import Report
from app.models.report_share import ReportShare
from app.models.step import Step
from app.models.tool_execution import ToolExecution
from app.models.widget import Widget

from ._cast import chinook_agent, h, two_orgs  # noqa: F401  (fixtures)


# ── report sharing ───────────────────────────────────────────────────────────

def _share(test_client, c, report_id, user_ids, share_type="conversation"):
    return test_client.put(
        f"/api/reports/{report_id}/visibility/{share_type}",
        json={"visibility": "shared", "shared_user_ids": user_ids},
        headers=h(c["a_token"], c["a_org"]),
    )


def _shares(test_client, c, report_id, share_type="conversation"):
    r = test_client.get(f"/api/reports/{report_id}/shares/{share_type}", headers=h(c["a_token"], c["a_org"]))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.e2e
def test_a_report_can_only_be_shared_with_members_of_its_org(test_client, two_orgs, create_report, whoami):
    c = two_orgs
    report = create_report(title="Depot throughput", user_token=c["a_token"], org_id=c["a_org"])
    outsider_email = whoami(c["b_token"])["email"]

    refused = _share(test_client, c, report["id"], [c["b_id"]])
    assert refused.status_code == 400, refused.text
    # A mixed list is refused as a whole: the member is not shared with either.
    refused = _share(test_client, c, report["id"], [c["a_member_id"], c["b_id"]])
    assert refused.status_code == 400, refused.text
    listed = _shares(test_client, c, report["id"])
    assert listed == []
    assert outsider_email not in str(listed)

    ok = _share(test_client, c, report["id"], [c["a_member_id"]])
    assert ok.status_code == 200, ok.text
    assert ok.json()["shared_user_ids"] == [c["a_member_id"]]
    token = ok.json()["conversation_share_token"]
    assert [s["user_id"] for s in _shares(test_client, c, report["id"])] == [c["a_member_id"]]
    # The member reads it; someone outside the organization does not.
    assert test_client.get(f"/api/c/{token}", headers=h(c["a_member_token"], c["a_org"])).status_code == 200
    assert test_client.get(f"/api/c/{token}", headers=h(c["b_token"], c["b_org"])).status_code in (403, 404)


async def _seed_share(report_id, user_id, share_type):
    # A share row for someone outside the organization can no longer be
    # written through the API; seeded directly to cover rows stored earlier.
    async with async_session_maker() as db:
        db.add(ReportShare(report_id=report_id, user_id=user_id, share_type=share_type))
        await db.commit()


@pytest.mark.e2e
@pytest.mark.parametrize("share_type", ["conversation", "artifact"])
def test_a_stored_share_grants_nothing_to_someone_outside_the_org(
    test_client, two_orgs, create_report, whoami, share_type,
):
    c = two_orgs
    report = create_report(title="Depot throughput", user_token=c["a_token"], org_id=c["a_org"])
    ok = _share(test_client, c, report["id"], [c["a_member_id"]], share_type)
    assert ok.status_code == 200, ok.text
    asyncio.run(_seed_share(report["id"], c["b_id"], share_type))

    if share_type == "conversation":
        url = f"/api/c/{ok.json()['conversation_share_token']}"
    else:
        url = f"/api/r/{report['id']}"
    assert test_client.get(url, headers=h(c["a_member_token"], c["a_org"])).status_code == 200
    denied = test_client.get(url, headers=h(c["b_token"], c["b_org"]))
    assert denied.status_code in (403, 404), denied.text
    assert "Depot throughput" not in denied.text

    # The outsider is not listed (no name, no email) ...
    listed = _shares(test_client, c, report["id"], share_type)
    assert [s["user_id"] for s in listed] == [c["a_member_id"]]
    assert whoami(c["b_token"])["email"] not in str(listed)
    # ... and a save that re-sends the stored list still goes through,
    # leaving only the member.
    resave = _share(test_client, c, report["id"], [c["a_member_id"], c["b_id"]], share_type)
    assert resave.status_code == 200, resave.text
    assert resave.json()["shared_user_ids"] == [c["a_member_id"]]


# ── external platform on report creation ─────────────────────────────────────

async def _seed_platform(org_id, team_name):
    # Platforms are created by the Slack/Teams/email install flows, which need
    # the real provider; the row is seeded directly.
    async with async_session_maker() as db:
        p = ExternalPlatform(organization_id=org_id, platform_type="slack",
                             platform_config={"team_id": f"T{uuid.uuid4().hex[:8]}", "team_name": team_name},
                             is_active=True)
        db.add(p)
        await db.commit()
        return str(p.id)


@pytest.mark.e2e
def test_a_report_can_only_be_tied_to_a_platform_of_its_org(test_client, two_orgs):
    c = two_orgs
    other_team, own_team = f"other-team-{uuid.uuid4().hex[:8]}", f"own-team-{uuid.uuid4().hex[:8]}"
    other_platform = asyncio.run(_seed_platform(c["b_org"], other_team))
    own_platform = asyncio.run(_seed_platform(c["a_org"], own_team))

    def _create(platform_id):
        return test_client.post(
            "/api/reports",
            json={"title": "From chat", "files": [], "data_sources": [], "external_platform_id": platform_id},
            headers=h(c["a_token"], c["a_org"]))

    refused = _create(other_platform)
    assert refused.status_code == 404, refused.text
    assert other_team not in refused.text

    ok = _create(own_platform)
    assert ok.status_code == 200, ok.text
    assert ok.json()["external_platform"]["id"] == own_platform


# ── fork ─────────────────────────────────────────────────────────────────────

async def _fork_count(report_id):
    async with async_session_maker() as db:
        return len((await db.execute(select(Report.id).where(Report.forked_from_id == report_id))).all())


@pytest.mark.e2e
def test_fork_acts_in_the_organization_of_the_request(test_client, two_orgs, create_report, publish_report):
    c = two_orgs
    # A's plain member also runs an organization of their own.
    created = test_client.post("/api/organizations", json={"name": f"side_org_{uuid.uuid4().hex[:8]}"},
                               headers={"Authorization": f"Bearer {c['a_member_token']}"})
    assert created.status_code == 200, created.text
    side_org = created.json()["id"]
    report = create_report(title="Published in A", user_token=c["a_token"], org_id=c["a_org"])
    publish_report(report["id"], user_token=c["a_token"], org_id=c["a_org"])

    def _count():
        return asyncio.run(_fork_count(report["id"]))

    before = _count()
    # Acting in their own organization, where A's report does not exist.
    refused = test_client.post(f"/api/reports/{report['id']}/fork", json={}, headers=h(c["a_member_token"], side_org))
    assert refused.status_code == 404, refused.text
    assert _count() == before

    # Acting in A, where the report lives.
    ok = test_client.post(f"/api/reports/{report['id']}/fork", json={}, headers=h(c["a_member_token"], c["a_org"]))
    assert ok.status_code == 200, ok.text
    assert ok.json()["forked_from_id"] == report["id"]
    assert _count() == before + 1


# ── email attachment by query id ─────────────────────────────────────────────

async def _seed_query_with_result(org_id, report_id=None):
    # Queries and their result steps are only produced by an agent run.
    async with async_session_maker() as db:
        q = Query(title=f"q_{uuid.uuid4().hex[:6]}", organization_id=org_id, report_id=report_id)
        db.add(q)
        await db.flush()
        step = Step(title="s", slug=f"s-{uuid.uuid4().hex[:8]}", status="success", query_id=q.id,
                    data={"rows": [{"depot": "north", "units": 7}],
                          "columns": [{"field": "depot", "headerName": "depot"},
                                      {"field": "units", "headerName": "units"}]},
                    data_model={"type": "table"})
        db.add(step)
        await db.flush()
        q.default_step_id = step.id
        await db.commit()
        return str(q.id)


async def _attach_query(query_id, report_id, org_id):
    from app.ai.tools.schemas.send_email import EmailAttachmentSpec
    from app.services.email_send_service import EmailSendService

    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        org = await db.get(Organization, org_id)
        res, att, tmp = await EmailSendService().resolve_attachment(
            EmailAttachmentSpec(ref_type="query", ref_id=query_id), db, report, org)
        return bool(res.success and att is not None)


@pytest.mark.e2e
def test_email_attachment_only_exports_queries_of_the_sending_org(test_client, two_orgs, create_report):
    c = two_orgs
    report = create_report(title="Sender", user_token=c["a_token"], org_id=c["a_org"])
    # With and without a parent report, another organization's query is refused.
    other_report = create_report(title="Other", user_token=c["b_token"], org_id=c["b_org"])
    for other_query in (
        asyncio.run(_seed_query_with_result(c["b_org"])),
        asyncio.run(_seed_query_with_result(c["b_org"], other_report["id"])),
        asyncio.run(_seed_query_with_result(None)),  # no organization at all
    ):
        assert asyncio.run(_attach_query(other_query, report["id"], c["a_org"])) is False

    for own_query in (
        asyncio.run(_seed_query_with_result(c["a_org"])),
        asyncio.run(_seed_query_with_result(c["a_org"], report["id"])),
    ):
        assert asyncio.run(_attach_query(own_query, report["id"], c["a_org"])) is True


# ── tool_execution_id on a query run ─────────────────────────────────────────

async def _seed_query_on_report(report_id, org_id, user_id):
    async with async_session_maker() as db:
        widget = Widget(title="w", slug=f"w-{uuid.uuid4().hex[:8]}", report_id=report_id)
        db.add(widget)
        await db.flush()
        q = Query(title="q", report_id=report_id, widget_id=widget.id, organization_id=org_id, user_id=user_id)
        db.add(q)
        await db.commit()
        return str(q.id)


async def _seed_tool_execution(report_id, user_id, org_id):
    # Tool executions are only written by the agent loop.
    async with async_session_maker() as db:
        completion = Completion(prompt={"content": "x"}, completion={"content": "y"}, role="system",
                                status="success", report_id=report_id, user_id=user_id)
        db.add(completion)
        await db.flush()
        ae = AgentExecution(completion_id=completion.id, report_id=report_id, organization_id=org_id)
        db.add(ae)
        await db.flush()
        te = ToolExecution(agent_execution_id=ae.id, tool_name="create_data", arguments_json={})
        db.add(te)
        await db.commit()
        return str(te.id)


async def _created_step_of(tool_execution_id):
    async with async_session_maker() as db:
        return (await db.execute(
            select(ToolExecution.created_step_id).where(ToolExecution.id == tool_execution_id)
        )).scalar_one()


@pytest.mark.e2e
def test_a_query_run_only_updates_a_tool_execution_of_its_own_report(test_client, two_orgs, create_report):
    c = two_orgs
    report = create_report(title="Mine", user_token=c["a_token"], org_id=c["a_org"])
    query_id = asyncio.run(_seed_query_on_report(report["id"], c["a_org"], c["a_id"]))
    other_report = create_report(title="Theirs", user_token=c["b_token"], org_id=c["b_org"])
    sibling_report = create_report(title="Mine too", user_token=c["a_token"], org_id=c["a_org"])
    foreign = [
        asyncio.run(_seed_tool_execution(other_report["id"], c["b_id"], c["b_org"])),
        asyncio.run(_seed_tool_execution(sibling_report["id"], c["a_id"], c["a_org"])),
    ]
    own = asyncio.run(_seed_tool_execution(report["id"], c["a_id"], c["a_org"]))

    def _run(tool_execution_id):
        r = test_client.post(
            f"/api/queries/{query_id}/run",
            json={"code": "def generate_df(ds_clients, excel_files):\n    import pandas as pd\n"
                          "    return pd.DataFrame({'a': [1]})\n",
                  "tool_execution_id": tool_execution_id},
            headers=h(c["a_token"], c["a_org"]))
        assert r.status_code == 200, r.text
        return r.json()["step"]["id"]

    for te in foreign:
        _run(te)
        assert asyncio.run(_created_step_of(te)) is None
    step_id = _run(own)
    assert asyncio.run(_created_step_of(own)) == step_id


# ── principals on role assignments, resource grants and agent members ────────

@pytest.mark.e2e
def test_access_can_only_be_given_to_principals_of_the_org(test_client, two_orgs, chinook_agent):
    c = two_orgs
    a = h(c["a_token"], c["a_org"])
    ds = chinook_agent(user_token=c["a_token"], org_id=c["a_org"])
    roles = test_client.get(f"/api/organizations/{c['a_org']}/roles", headers=a).json()
    member_role = next(r for r in roles if r["name"] == "member")

    def _assign(user_id):
        return test_client.post(f"/api/organizations/{c['a_org']}/role-assignments", headers=a, json={
            "role_id": member_role["id"], "principal_type": "user", "principal_id": user_id})

    def _grant(user_id):
        return test_client.post(f"/api/organizations/{c['a_org']}/resource-grants", headers=a, json={
            "resource_type": "data_source", "resource_id": ds["id"],
            "principal_type": "user", "principal_id": user_id, "permissions": ["view"]})

    def _add_member(user_id):
        return test_client.post(f"/api/data_sources/{ds['id']}/members", headers=a, json={
            "principal_type": "user", "principal_id": user_id})

    def _set_members(user_ids):
        return test_client.put(f"/api/data_sources/{ds['id']}", headers=a, json={"member_user_ids": user_ids})

    for attempt in (_assign, _grant, _add_member):
        refused = attempt(c["b_id"])
        assert refused.status_code == 404, (attempt.__name__, refused.text)
    refused = _set_members([c["a_member_id"], c["b_id"]])
    assert refused.status_code == 404, refused.text

    members = test_client.get(f"/api/data_sources/{ds['id']}/members", headers=a)
    assert members.status_code == 200, members.text
    assert c["b_id"] not in members.text
    grants = test_client.get(f"/api/organizations/{c['a_org']}/resource-grants", headers=a)
    assert c["b_id"] not in grants.text
    assignments = test_client.get(f"/api/organizations/{c['a_org']}/role-assignments", headers=a)
    assert c["b_id"] not in assignments.text

    # A member of the organization is accepted by each of them.
    ok = _grant(c["a_member_id"])
    assert ok.status_code == 200, ok.text
    ok = _set_members([c["a_member_id"]])
    assert ok.status_code == 200, ok.text
    members = test_client.get(f"/api/data_sources/{ds['id']}/members", headers=a).json()
    assert c["a_member_id"] in {m["principal_id"] for m in members}
    removed = test_client.delete(f"/api/data_sources/{ds['id']}/members/{c['a_member_id']}", headers=a)
    assert removed.status_code == 204, removed.text
    ok = _add_member(c["a_member_id"])
    assert ok.status_code == 200, ok.text
    # The member already holds the member role; a second assignment conflicts
    # rather than being refused as unknown.
    assert _assign(c["a_member_id"]).status_code in (200, 409)


# ── agent YAML: members on import and export ─────────────────────────────────

async def _seed_agent_membership(data_source_id, user_id):
    # A member row for someone outside the organization can no longer be
    # written through the API; seeded directly to cover rows stored earlier.
    from app.models.data_source_membership import DataSourceMembership

    async with async_session_maker() as db:
        db.add(DataSourceMembership(data_source_id=data_source_id, principal_type="user", principal_id=user_id))
        await db.commit()


@pytest.mark.e2e
def test_agent_yaml_names_only_members_of_the_org(test_client, two_orgs, chinook_agent, whoami):
    c = two_orgs
    a = h(c["a_token"], c["a_org"])
    ds = chinook_agent(user_token=c["a_token"], org_id=c["a_org"])
    conn_name = ds["connections"][0]["name"]
    outsider_email = whoami(c["b_token"])["email"]
    member_email = whoami(c["a_member_token"])["email"]

    def _apply(name, email):
        manifest = f'name: {name}\nis_public: false\nconnections:\n  - "{conn_name}"\nmembers:\n  - user: {email}\n'
        r = test_client.post("/api/agents/apply", content=manifest,
                             headers={**a, "Content-Type": "application/yaml"})
        assert r.status_code == 200, r.text
        return r.json()

    def _export(name):
        r = test_client.get(f"/api/agents/{name}.yaml", headers=a)
        assert r.status_code == 200, r.text
        return r.text

    # Import: someone outside the organization cannot be named as a member.
    refused = _apply("outsider-agent", outsider_email)
    assert refused["status"] == "error", refused
    assert "user_not_found" in {e["code"] for e in refused["errors"]}

    created = _apply("member-agent", member_email)
    assert created["status"] == "created", created
    assert member_email in _export("member-agent")

    # Export: a stored member row for an outsider is not turned into an email.
    agents = test_client.get("/api/data_sources", headers=a).json()
    agent_id = next(d["id"] for d in agents if d["name"] == "member-agent")
    asyncio.run(_seed_agent_membership(agent_id, c["b_id"]))
    exported = _export("member-agent")
    assert outsider_email not in exported
    assert member_email in exported
