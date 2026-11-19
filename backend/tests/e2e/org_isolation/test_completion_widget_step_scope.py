"""A completion may only reference a widget / step of the report it is posted to.

Contract: `prompt.widget_id` and `prompt.step_id` are accepted only when they
belong to that report; any other id — another organization's, or another
report's in the same organization — is answered exactly like an unknown id
(404), on every route that takes a prompt. Reading a widget through the widget
service is bound to the caller's organization.

Widgets and steps are produced by the agent loop, so they are seeded directly.
"""
import asyncio
import uuid

import pytest
from fastapi import HTTPException

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.query import Query
from app.models.step import Step
from app.models.user import User
from app.models.widget import Widget
from app.services.widget_service import WidgetService

from .agent_surface_fixtures import allow_multiple_orgs, h, two_orgs  # noqa: F401


async def _seed_widget(report_id, org_id, user_id):
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        widget = Widget(title=f"w-{suffix}", slug=f"w-{suffix}", report_id=report_id)
        db.add(widget)
        await db.flush()
        query = Query(title="q", report_id=report_id, widget_id=widget.id,
                      organization_id=org_id, user_id=user_id)
        db.add(query)
        await db.flush()
        step = Step(title="s", slug=f"s-{suffix}", status="success", widget_id=widget.id,
                    query_id=query.id, code="select 1",
                    data={"rows": [{"marker": f"rows-{suffix}"}], "columns": [{"field": "marker"}]},
                    data_model={"type": "table"})
        db.add(step)
        await db.commit()
        return {"widget_id": str(widget.id), "step_id": str(step.id), "marker": f"rows-{suffix}"}


@pytest.fixture
def reports(two_orgs, create_report):
    o = two_orgs
    own = create_report(title="own", user_token=o["home_token"], org_id=o["home_org"])
    sibling = create_report(title="sibling", user_token=o["home_token"], org_id=o["home_org"])
    foreign = create_report(title="foreign", user_token=o["other_token"], org_id=o["other_org"])
    return {
        **o, "report_id": own["id"],
        "own": asyncio.run(_seed_widget(own["id"], o["home_org"], o["home_user_id"])),
        "sibling": asyncio.run(_seed_widget(sibling["id"], o["home_org"], o["home_user_id"])),
        "foreign": asyncio.run(_seed_widget(foreign["id"], o["other_org"], o["other_user_id"])),
    }


ROUTES = ("completions/estimate", "completions")


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("owner", ["foreign", "sibling"])
@pytest.mark.parametrize("field", ["widget_id", "step_id"])
def test_widget_or_step_outside_the_report_is_not_found(test_client, reports, route, owner, field):
    r = reports
    headers = h(r["home_token"], r["home_org"])
    url = f"/api/reports/{r['report_id']}/{route}"

    resp = test_client.post(url, headers=headers,
                            json={"prompt": {"content": "hi", field: r[owner][field]}})
    unknown = test_client.post(url, headers=headers,
                               json={"prompt": {"content": "hi", field: str(uuid.uuid4())}})

    assert resp.status_code == 404, resp.text
    # Indistinguishable from an id that does not exist.
    assert (resp.status_code, resp.json()) == (unknown.status_code, unknown.json())
    assert r[owner]["marker"] not in resp.text


@pytest.mark.parametrize("field", ["widget_id", "step_id"])
def test_widget_and_step_of_the_report_are_accepted(test_client, reports, field):
    r = reports
    resp = test_client.post(
        f"/api/reports/{r['report_id']}/completions/estimate",
        headers=h(r["home_token"], r["home_org"]),
        json={"prompt": {"content": "hi", field: r["own"][field]}},
    )
    # Validation passed: whatever the estimate answers, it is not "not found".
    assert resp.status_code != 404, resp.text


def test_no_completion_keeps_a_widget_from_outside_the_report(test_client, reports):
    r = reports
    headers = h(r["home_token"], r["home_org"])
    test_client.post(f"/api/reports/{r['report_id']}/completions", headers=headers,
                     json={"prompt": {"content": "hi", "widget_id": r["foreign"]["widget_id"]}})

    listed = test_client.get(f"/api/reports/{r['report_id']}/completions.legacy", headers=headers)
    assert listed.status_code == 200, listed.text
    assert r["foreign"]["marker"] not in listed.text
    assert r["foreign"]["widget_id"] not in listed.text


def test_widget_service_reads_only_within_the_callers_organization(reports):
    r = reports

    async def _read(widget_id, org_id, user_id):
        async with async_session_maker() as db:
            org = await db.get(Organization, org_id)
            user = await db.get(User, user_id)
            return await WidgetService().get_widget_by_id(db, widget_id, user, org)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_read(r["foreign"]["widget_id"], r["home_org"], r["home_user_id"]))
    assert exc.value.status_code == 404

    own = asyncio.run(_read(r["own"]["widget_id"], r["home_org"], r["home_user_id"]))
    assert str(own.id) == r["own"]["widget_id"]
    assert own.last_step is not None
