"""A viewer of one dashboard reads that dashboard's data only.

Dashboards of a report are shared one by one, but their data lives on the
report's queries. The share page's data endpoints must follow the dashboards:

- /r/{id}/queries lists the queries behind the dashboards the caller may
  open, plus the query their filters draw options from — never another
  dashboard's query, even when asked for that dashboard's version;
- /r/{id}/queries/{qid}/step answers 404 for another dashboard's query, the
  same as for a query that does not exist;
- /r/{id} does not list the report's widgets to such a viewer;
- the owner, and a viewer the whole conversation is shared with, still read
  every query;
- the agent's notes (they cover every dashboard) need the whole
  conversation, and who each dashboard is shared with stays the owner's;
- the signed-in step / CSV / visualization reads and the MCP
  get_visualization tool follow the same per-dashboard rule;
- the report card in listings shows only the dashboards the caller may
  open: thumbnail, count, modes, no widget titles.

Run:
    cd backend
    TESTING=true uv run pytest tests/e2e/rbac/test_per_dashboard_data_scope.py --db=sqlite
"""
import asyncio
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.visualization import Visualization
from app.models.widget import Widget
from tests.fixtures.artifact import seed_artifact


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


async def _seed(report_id):
    """Two dashboards with a query each; Sales' query has a filter whose
    options come from a third query that no chart shows."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        org_id, user_id = report.organization_id, report.user_id

        async def query(title, parameters=None):
            widget = Widget(title=title, slug=f"{title}-{suffix}", report_id=report_id)
            db.add(widget)
            await db.flush()
            q = Query(title=title, report_id=report_id, widget_id=widget.id,
                      organization_id=org_id, user_id=user_id, parameters=parameters or [])
            db.add(q)
            await db.flush()
            step = Step(title=title, slug=f"{title}-step-{suffix}", status="success", widget_id=widget.id,
                        query_id=q.id, data={"rows": [{"v": title}], "columns": []}, data_model={"type": "table"})
            db.add(step)
            await db.flush()
            q.default_step_id = step.id
            viz = Visualization(title=title, status="success", report_id=report_id,
                                query_id=q.id, view={"type": "table"})
            db.add(viz)
            await db.flush()
            steps[title] = str(step.id)
            return str(q.id), str(viz.id)

        steps = {}
        regions_q, _ = await query("Regions")
        sales_q, sales_viz = await query("Sales", parameters=[{
            "name": "region", "type": "string",
            "options_source": {"query_id": regions_q, "value_column": "v"},
        }])
        payroll_q, payroll_viz = await query("Payroll")

        sales = await seed_artifact(db, report_id=report_id, user_id=user_id, organization_id=org_id,
                                    title="Sales", content={"code": "<div/>", "visualization_ids": [sales_viz]},
                                    thumbnail_path=f"thumbnails/sales-{suffix}.png")
        payroll = await seed_artifact(db, report_id=report_id, user_id=user_id, organization_id=org_id,
                                      title="Payroll", content={"code": "<div/>", "visualization_ids": [payroll_viz]},
                                      thumbnail_path=f"thumbnails/payroll-{suffix}.png")
        await db.commit()
        return {
            "sales": {"artifact_id": str(sales.artifact_id), "version_id": str(sales.id), "query": sales_q,
                      "step": steps["Sales"], "viz": sales_viz},
            "payroll": {"artifact_id": str(payroll.artifact_id), "version_id": str(payroll.id), "query": payroll_q,
                        "step": steps["Payroll"], "viz": payroll_viz},
            "regions_query": regions_q,
        }


@pytest.fixture
def scoped(test_client, create_user, login_user, whoami, invite_user_to_org, create_report):
    owner = create_user()
    owner_token = login_user(owner["email"], owner["password"])
    org_id = whoami(owner_token)["organizations"][0]["id"]
    viewer = invite_user_to_org(org_id=org_id, admin_token=owner_token)
    report = create_report(title="Scoped", user_token=owner_token, org_id=org_id, data_sources=[])
    seeded = asyncio.run(_seed(report["id"]))

    resp = test_client.put(
        f"/api/reports/{report['id']}/artifacts/{seeded['sales']['artifact_id']}/visibility",
        json={"visibility": "shared", "shared_user_ids": [viewer["user_id"]]},
        headers=_headers(owner_token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    return {"rid": report["id"], "org_id": org_id, "owner_token": owner_token, "viewer": viewer, **seeded}


def _query_ids(test_client, rid, headers, artifact_version=None):
    url = f"/api/r/{rid}/queries" + (f"?artifact_id={artifact_version}" if artifact_version else "")
    resp = test_client.get(url, headers=headers)
    assert resp.status_code == 200, resp.json()
    return {q["id"] for q in resp.json()}


@pytest.mark.e2e
def test_viewer_reads_only_the_shared_dashboards_queries(test_client, scoped):
    c = scoped
    viewer = _headers(c["viewer"]["token"], c["org_id"])

    assert _query_ids(test_client, c["rid"], viewer) == {c["sales"]["query"], c["regions_query"]}
    assert _query_ids(test_client, c["rid"], viewer, c["payroll"]["version_id"]) == set()

    ok = test_client.get(f"/api/r/{c['rid']}/queries/{c['sales']['query']}/step", headers=viewer)
    assert ok.status_code == 200, ok.json()
    assert test_client.get(f"/api/r/{c['rid']}/queries/{c['regions_query']}/step", headers=viewer).status_code == 200
    denied = test_client.get(f"/api/r/{c['rid']}/queries/{c['payroll']['query']}/step", headers=viewer)
    assert denied.status_code == 404

    report = test_client.get(f"/api/r/{c['rid']}", headers=viewer)
    assert report.status_code == 200, report.json()
    assert report.json()["widgets"] == []


@pytest.mark.e2e
def test_owner_and_conversation_viewer_read_every_query(test_client, scoped, set_visibility):
    c = scoped
    every = {c["sales"]["query"], c["payroll"]["query"], c["regions_query"]}
    owner = _headers(c["owner_token"], c["org_id"])
    assert _query_ids(test_client, c["rid"], owner) == every
    assert test_client.get(f"/api/r/{c['rid']}/queries/{c['payroll']['query']}/step", headers=owner).status_code == 200

    set_visibility(c["rid"], "conversation", "shared", user_token=c["owner_token"], org_id=c["org_id"],
                   shared_user_ids=[c["viewer"]["user_id"]])
    viewer = _headers(c["viewer"]["token"], c["org_id"])
    assert _query_ids(test_client, c["rid"], viewer) == every
    assert test_client.get(f"/api/r/{c['rid']}/queries/{c['payroll']['query']}/step", headers=viewer).status_code == 200


@pytest.mark.e2e
def test_agent_notes_need_the_whole_conversation(test_client, scoped, set_visibility):
    """Agent notes cover every dashboard of the report, so opening one
    dashboard is not enough to read them."""
    c = scoped
    notes = f"/api/reports/{c['rid']}/notes"
    assert test_client.get(notes, headers=_headers(c["owner_token"], c["org_id"])).status_code == 200
    viewer = _headers(c["viewer"]["token"], c["org_id"])
    assert test_client.get(notes, headers=viewer).status_code == 404

    set_visibility(c["rid"], "conversation", "shared", user_token=c["owner_token"], org_id=c["org_id"],
                   shared_user_ids=[c["viewer"]["user_id"]])
    assert test_client.get(notes, headers=viewer).status_code == 200


@pytest.mark.e2e
def test_viewer_does_not_learn_who_dashboards_are_shared_with(test_client, scoped):
    c = scoped
    owner = test_client.get(f"/api/reports/{c['rid']}", headers=_headers(c["owner_token"], c["org_id"])).json()
    assert c["viewer"]["user_id"] in owner["artifact_shared_user_ids"]

    resp = test_client.get(f"/api/reports/{c['rid']}", headers=_headers(c["viewer"]["token"], c["org_id"]))
    assert resp.status_code == 200, resp.json()
    assert resp.json()["artifact_shared_user_ids"] == []
    assert resp.json()["artifact_shared_group_ids"] == []


@pytest.mark.e2e
def test_step_csv_and_visualization_reads_follow_the_dashboards(test_client, scoped):
    c = scoped
    viewer = _headers(c["viewer"]["token"], c["org_id"])
    owner = _headers(c["owner_token"], c["org_id"])

    assert test_client.get(f"/api/steps/{c['sales']['step']}", headers=viewer).status_code == 200
    assert test_client.get(f"/api/visualizations/{c['sales']['viz']}", headers=viewer).status_code == 200
    assert test_client.get(f"/api/steps/{c['payroll']['step']}", headers=viewer).status_code == 403
    assert test_client.get(f"/api/steps/{c['payroll']['step']}/export", headers=viewer).status_code == 403
    assert test_client.get(f"/api/visualizations/{c['payroll']['viz']}", headers=viewer).status_code == 403
    assert test_client.get(f"/api/steps/{c['payroll']['step']}", headers=owner).status_code == 200


@pytest.mark.e2e
def test_mcp_get_visualization_follows_the_dashboards(scoped):
    from app.ai.tools.mcp.app_tools import GetVisualizationMCPTool
    from app.models.organization import Organization
    from app.models.user import User

    c = scoped

    async def call(viz_id):
        async with async_session_maker() as db:
            user = await db.get(User, c["viewer"]["user_id"])
            org = await db.get(Organization, c["org_id"])
            return await GetVisualizationMCPTool().execute({"visualization_id": viz_id}, db, user, org)

    assert "error" not in asyncio.run(call(c["sales"]["viz"]))
    denied = asyncio.run(call(c["payroll"]["viz"]))
    assert "error" in denied and "rows" not in str(denied)


@pytest.mark.e2e
def test_report_card_shows_only_the_shared_dashboard(test_client, scoped, list_reports):
    c = scoped
    listed = list_reports(user_token=c["viewer"]["token"], org_id=c["org_id"], filter="shared")["reports"]
    card = next(r for r in listed if r["id"] == c["rid"])
    # Payroll is newer and would win the thumbnail pick; it is private.
    assert card["thumbnail_url"] and "sales-" in card["thumbnail_url"], card["thumbnail_url"]
    assert card["artifact_count"] == 1
    assert card["widgets"] == []

    owned = list_reports(user_token=c["owner_token"], org_id=c["org_id"])["reports"]
    own_card = next(r for r in owned if r["id"] == c["rid"])
    assert own_card["artifact_count"] == 2
    assert "payroll-" in own_card["thumbnail_url"]
