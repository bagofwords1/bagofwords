"""The scheduled run covers every dashboard of a report.

Dashboards are shared one by one, so the schedule — still set once per
report — must not leave any of them behind:

- the scheduled rerun refreshes the queries behind every live dashboard/deck
  (not only the newest one), while an interactive refresh of one dashboard
  stays scoped to it;
- the scheduled email lists one link per dashboard the subscriber may open,
  so two subscribers with different access get different lists, an address
  that is not a user only gets the public dashboards, and a subscriber who
  may open none falls back to the single report link.

Run:
    cd backend
    TESTING=true uv run pytest tests/e2e/rbac/test_scheduled_run_every_dashboard.py --db=sqlite
"""
import asyncio
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.user import User
from app.models.visualization import Visualization
from app.models.widget import Widget
from tests.fixtures.artifact import seed_artifact


async def _seed_report_with_dashboards(n: int):
    """A report whose dashboards each show their own query, plus a deleted one."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Schedule Org {suffix}")
        db.add(org)
        await db.flush()
        user = User(name="Owner", email=f"schedule-{suffix}@example.com", hashed_password="x",
                    is_active=True, is_superuser=False, is_verified=True)
        db.add(user)
        await db.flush()
        report = Report(title="Schedule", slug=f"schedule-{suffix}", organization_id=org.id, user_id=user.id)
        db.add(report)
        await db.flush()

        dashboards = []
        for i in range(n + 1):
            widget = Widget(title=f"W{i}", slug=f"w{i}-{suffix}", report_id=report.id)
            db.add(widget)
            await db.flush()
            step = Step(title=f"S{i}", slug=f"s{i}-{suffix}", status="success", widget_id=widget.id,
                        data={"rows": [], "columns": []}, data_model={"type": "table"})
            db.add(step)
            await db.flush()
            query = Query(title=f"Q{i}", report_id=report.id, widget_id=widget.id, default_step_id=step.id)
            db.add(query)
            await db.flush()
            viz = Visualization(title=f"V{i}", status="success", report_id=report.id,
                                query_id=query.id, view={"type": "table"})
            db.add(viz)
            await db.flush()
            version = await seed_artifact(
                db, report_id=report.id, user_id=user.id, organization_id=org.id,
                title=f"Dashboard {i}", content={"code": "<div/>", "visualization_ids": [str(viz.id)]},
            )
            dashboards.append({"version_id": str(version.id), "query_id": str(query.id)})

        # The last one is deleted: a scheduled run must not refresh it.
        from app.services.artifact_service import ArtifactService
        await ArtifactService().delete(db, dashboards[-1]["version_id"])
        await db.commit()
        return str(report.id), dashboards[:-1], dashboards[-1]


@pytest.mark.e2e
def test_scheduled_run_refreshes_every_live_dashboard():
    from app.services.report_service import ReportService

    report_id, live, deleted = asyncio.run(_seed_report_with_dashboards(3))

    async def query_ids(**kwargs):
        async with async_session_maker() as db:
            service = ReportService()
            if kwargs.get("all"):
                return await service._all_artifacts_query_ids(db, report_id)
            return await service._artifact_query_ids(db, report_id, kwargs.get("artifact_id"))

    assert set(asyncio.run(query_ids(all=True))) == {d["query_id"] for d in live}
    # An interactive refresh of one dashboard still refreshes that one only.
    assert asyncio.run(query_ids(artifact_id=live[0]["version_id"])) == [live[0]["query_id"]]
    assert deleted["query_id"] not in asyncio.run(query_ids(all=True))


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.mark.e2e
def test_scheduled_email_lists_the_dashboards_each_subscriber_may_open(
    test_client, create_user, login_user, whoami, invite_user_to_org, create_report,
):
    from app.services.notification_service import notification_service

    owner = create_user()
    owner_token = login_user(owner["email"], owner["password"])
    org_id = whoami(owner_token)["organizations"][0]["id"]
    sales_viewer = invite_user_to_org(org_id=org_id, admin_token=owner_token)
    no_access = invite_user_to_org(org_id=org_id, admin_token=owner_token)
    report = create_report(title="Weekly", user_token=owner_token, org_id=org_id, data_sources=[])
    rid = report["id"]

    def dashboard(title):
        resp = test_client.post("/api/artifacts", json={
            "report_id": rid, "title": title, "mode": "page", "content": {"code": "<div/>"},
        }, headers=_headers(owner_token, org_id))
        assert resp.status_code == 200, resp.json()
        return resp.json()["artifact_id"]

    def share(artifact_id, body):
        resp = test_client.put(f"/api/reports/{rid}/artifacts/{artifact_id}/visibility",
                               json=body, headers=_headers(owner_token, org_id))
        assert resp.status_code == 200, resp.json()

    public_id, sales_id, private_id = dashboard("Public KPIs"), dashboard("Sales"), dashboard("Private")
    share(public_id, {"visibility": "public"})
    share(sales_id, {"visibility": "shared", "shared_user_ids": [sales_viewer["user_id"]]})

    report_url = f"https://bow.example/r/{rid}"
    subscribers = [
        {"type": "user", "id": sales_viewer["user_id"]},
        {"type": "user", "id": no_access["user_id"]},
        {"type": "email", "address": "outside@example.com"},
        {"type": "user", "id": whoami(owner_token)["id"]},
    ]
    groups = asyncio.run(notification_service._group_subscribers_by_dashboards(rid, report_url, subscribers))
    by_subscriber = {
        (s.get("id") or s.get("address")): {url.split("artifact=")[1] for _, url in links}
        for links, subs in groups.items() for s in subs
    }

    assert by_subscriber[sales_viewer["user_id"]] == {public_id, sales_id}
    # Org members see 'public' too; nothing else was shared with this one.
    assert by_subscriber[no_access["user_id"]] == {public_id}
    assert by_subscriber["outside@example.com"] == {public_id}
    assert by_subscriber[whoami(owner_token)["id"]] == {public_id, sales_id, private_id}

    # Rendered email: one link per dashboard, the report link only as fallback.
    from app.services.email_renderer import render_scheduled_prompt_email
    _, html = render_scheduled_prompt_email(
        "en", report_title="Weekly", report_url=report_url,
        dashboard_links=[{"title": "Sales <Q3>", "url": f"{report_url}?artifact={sales_id}"}],
    )
    assert f"{report_url}?artifact={sales_id}" in html
    assert "Sales &lt;Q3&gt;" in html
    assert f'href="{report_url}"' not in html
    _, fallback = render_scheduled_prompt_email("en", report_title="Weekly", report_url=report_url)
    assert f'href="{report_url}"' in fallback
