"""
Regression tests for the cross-org read leak in query reads (issue #453).

An org-A member must never be able to read another org's query, its default
step, or its artifacts by enumerating/guessing UUIDs. The fix binds the
resource->org check at the route decorator (``model=Query`` / ``model=Report``
with a recognized id param) AND threads ``organization_id`` into the backing
service reads as defense in depth.

These tests assert both halves:
  * Route tier — an org-A caller hitting an org-B query/report id gets 404
    (not 403, to avoid existence disclosure) from every affected route.
  * Service tier — the service reads are org-scoped on their own, independent
    of the decorator.
"""
import asyncio
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.report import Report
from app.models.widget import Widget
from app.models.step import Step
from app.models.query import Query
from app.services.query_service import QueryService


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def two_orgs(bootstrap_admin, create_report):
    """Two independent orgs (A and B). Org B owns a report + query."""
    org_a = bootstrap_admin("orga")
    org_b = bootstrap_admin("orgb")

    report_b = create_report(
        title="Org B report",
        user_token=org_b["token"],
        org_id=org_b["org_id"],
    )

    return {"org_a": org_a, "org_b": org_b, "report_b": report_b}


@pytest.fixture
def org_b_query(test_client, two_orgs):
    org_b = two_orgs["org_b"]
    report_b = two_orgs["report_b"]
    resp = test_client.post(
        "/api/queries",
        json={"title": "Org B query", "report_id": report_b["id"]},
        headers=_hdr(org_b["token"], org_b["org_id"]),
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


# ────────────────────────────────────────────────────────────────────────
# Route tier — every affected query route must 404 across orgs.
# ────────────────────────────────────────────────────────────────────────


def test_get_query_cross_org_returns_404(test_client, two_orgs, org_b_query):
    org_a = two_orgs["org_a"]
    qid = org_b_query["id"]

    resp = test_client.get(
        f"/api/queries/{qid}",
        headers=_hdr(org_a["token"], org_a["org_id"]),
    )
    assert resp.status_code == 404, resp.json()


def test_default_step_cross_org_returns_404(test_client, two_orgs, org_b_query):
    org_a = two_orgs["org_a"]
    qid = org_b_query["id"]

    resp = test_client.get(
        f"/api/queries/{qid}/default_step",
        headers=_hdr(org_a["token"], org_a["org_id"]),
    )
    # Before the fix this returned 200 with the org-B step payload.
    assert resp.status_code == 404, resp.json()


def test_run_query_new_step_cross_org_returns_404(test_client, two_orgs, org_b_query):
    org_a = two_orgs["org_a"]
    qid = org_b_query["id"]

    resp = test_client.post(
        f"/api/queries/{qid}/run",
        json={"code": "df = pd.DataFrame()"},
        headers=_hdr(org_a["token"], org_a["org_id"]),
    )
    assert resp.status_code == 404, resp.json()


def test_preview_cross_org_returns_404(test_client, two_orgs, org_b_query):
    org_a = two_orgs["org_a"]
    qid = org_b_query["id"]

    resp = test_client.post(
        f"/api/queries/{qid}/preview",
        json={"code": "df = pd.DataFrame()"},
        headers=_hdr(org_a["token"], org_a["org_id"]),
    )
    assert resp.status_code == 404, resp.json()


def test_artifacts_by_report_cross_org_returns_404(test_client, two_orgs):
    org_a = two_orgs["org_a"]
    report_b = two_orgs["report_b"]

    resp = test_client.get(
        f"/api/artifacts/report/{report_b['id']}",
        headers=_hdr(org_a["token"], org_a["org_id"]),
    )
    assert resp.status_code == 404, resp.json()


def test_same_org_access_still_works(test_client, two_orgs, org_b_query):
    """Sanity: the owning org keeps full access (no over-blocking)."""
    org_b = two_orgs["org_b"]
    qid = org_b_query["id"]

    get_resp = test_client.get(
        f"/api/queries/{qid}",
        headers=_hdr(org_b["token"], org_b["org_id"]),
    )
    assert get_resp.status_code == 200, get_resp.json()
    assert get_resp.json()["id"] == qid

    step_resp = test_client.get(
        f"/api/queries/{qid}/default_step",
        headers=_hdr(org_b["token"], org_b["org_id"]),
    )
    assert step_resp.status_code == 200, step_resp.json()

    art_resp = test_client.get(
        f"/api/artifacts/report/{two_orgs['report_b']['id']}",
        headers=_hdr(org_b["token"], org_b["org_id"]),
    )
    assert art_resp.status_code == 200, art_resp.json()


# ────────────────────────────────────────────────────────────────────────
# Service tier — reads are org-scoped on their own (defense in depth).
# ────────────────────────────────────────────────────────────────────────


def test_service_get_query_is_org_scoped(two_orgs, org_b_query):
    org_a = two_orgs["org_a"]
    org_b = two_orgs["org_b"]
    qid = org_b_query["id"]
    service = QueryService()

    async def _check():
        async with async_session_maker() as db:
            # Wrong org → no row.
            assert await service.get_query(db, qid, organization_id=org_a["org_id"]) is None
            # Right org → the row.
            owned = await service.get_query(db, qid, organization_id=org_b["org_id"])
            assert owned is not None and str(owned.id) == qid
            # default-step read is likewise org-blind only when scoped wrong.
            assert await service.get_default_step_for_query(
                db, qid, organization_id=org_a["org_id"]
            ) is None

    _run(_check())


def test_service_run_existing_step_is_org_scoped(two_orgs):
    """A step owned by org B cannot be rerun by an org-A caller."""
    org_a = two_orgs["org_a"]
    org_b = two_orgs["org_b"]
    service = QueryService()

    async def _build_and_check():
        suffix = uuid.uuid4().hex[:8]
        async with async_session_maker() as db:
            report = Report(
                title="svc report",
                slug=f"svc-report-{suffix}",
                user_id=str(org_b["user_id"]),
                organization_id=str(org_b["org_id"]),
            )
            db.add(report)
            await db.flush()

            widget = Widget(
                title="svc widget",
                slug=f"svc-widget-{suffix}",
                report_id=str(report.id),
            )
            db.add(widget)
            await db.flush()

            step = Step(
                title="svc step",
                slug=f"svc-step-{suffix}",
                widget_id=str(widget.id),
            )
            db.add(step)
            await db.commit()
            step_id = str(step.id)

        async with async_session_maker() as db:
            # Cross-org caller is rejected before any execution.
            with pytest.raises(ValueError):
                await service.run_existing_step(
                    db, step_id, organization_id=str(org_a["org_id"])
                )

    _run(_build_and_check())
