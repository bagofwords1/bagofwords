"""
A deleted artifact stops counting everywhere a report advertises artifacts.

Deleting an artifact's last live version soft-deletes its parent
(ArtifactService.delete). Every surface that reads the parent table must then
skip it — otherwise the report keeps a dashboard icon, a non-zero count and a
"has artifacts" match for something that no longer exists, and the list and
detail paths disagree on the count.

Run:
    cd backend
    TESTING=true uv run pytest tests/e2e/test_deleted_artifact_counts.py --db=sqlite
"""
import asyncio

import pytest

from app.dependencies import async_session_maker
from app.models.report import Report
from tests.fixtures.artifact import seed_artifact


def _run(coro):
    return asyncio.run(coro)


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


async def _seed_dashboard(report_id):
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        version = await seed_artifact(
            db,
            report_id=report_id,
            user_id=report.user_id,
            organization_id=report.organization_id,
            mode="page",
            title="Dashboard",
        )
        await db.commit()
        return str(version.id)


def _listed(test_client, headers, **params):
    query = "&".join(f"{k}={v}" for k, v in {"filter": "my", "limit": 100, **params}.items())
    response = test_client.get(f"/api/reports?{query}", headers=headers)
    assert response.status_code == 200, response.json()
    return {r["id"]: r for r in response.json()["reports"]}


def _surfaces(test_client, headers, report_id, project_id, get_project, token, org_id):
    """What every artifact-aware surface currently says about the report."""
    detail = test_client.get(f"/api/reports/{report_id}", headers=headers)
    assert detail.status_code == 200, detail.json()
    return {
        "list artifact_count": _listed(test_client, headers)[report_id]["artifact_count"],
        "list artifact_modes": _listed(test_client, headers)[report_id]["artifact_modes"],
        "sidebar artifact_modes": _listed(test_client, headers, view="minimal")[report_id]["artifact_modes"],
        "has_artifacts=yes matches": report_id in _listed(test_client, headers, has_artifacts="yes"),
        "has_artifacts=no matches": report_id in _listed(test_client, headers, has_artifacts="no"),
        "detail artifact_count": detail.json()["artifact_count"],
        "project dashboard_count": get_project(project_id, user_token=token, org_id=org_id).json()["dashboard_count"],
    }


@pytest.mark.e2e
def test_deleted_artifact_stops_counting_everywhere(
    test_client, create_user, login_user, whoami, create_report, create_project, get_project,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = _auth(token, org_id)
    project = create_project(name="Dashboards", user_token=token, org_id=org_id)
    report = create_report(
        title="Has a dashboard", user_token=token, org_id=org_id,
        data_sources=[], project_id=project["id"],
    )
    version_id = _run(_seed_dashboard(report["id"]))

    def surfaces():
        return _surfaces(test_client, headers, report["id"], project["id"], get_project, token, org_id)

    # Control: while the dashboard is live, every surface sees it — so the
    # assertions below can only pass because the delete was honoured.
    assert surfaces() == {
        "list artifact_count": 1,
        "list artifact_modes": ["page"],
        "sidebar artifact_modes": ["page"],
        "has_artifacts=yes matches": True,
        "has_artifacts=no matches": False,
        "detail artifact_count": 1,
        "project dashboard_count": 1,
    }

    deleted = test_client.delete(f"/api/artifacts/{version_id}", headers=headers)
    assert deleted.status_code == 200, deleted.json()

    assert surfaces() == {
        "list artifact_count": 0,
        "list artifact_modes": [],
        "sidebar artifact_modes": [],
        "has_artifacts=yes matches": False,
        "has_artifacts=no matches": True,
        "detail artifact_count": 0,
        "project dashboard_count": 0,
    }
