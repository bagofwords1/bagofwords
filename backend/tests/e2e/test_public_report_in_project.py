"""Regression: GET /api/r/{id} for a report that lives inside a project.

The published-dashboard page (/r/{id}) is what the project page's dashboard
cards link to. ``Report.project`` is a ``lazy="joined"`` to-one, and the
public loader turns eager loading off wholesale with ``lazyload("*")`` — so
serializing ``ReportSchema.project`` used to lazy-load on an async session
and blow up (MissingGreenlet -> 500), but only for reports that actually
carry a ``project_id``. The frontend maps any non-401/403 failure to
/not_found, which is why the card read as a 404.
"""
import uuid

import pytest


def _owner(create_user, login_user, whoami):
    email = f"pub_proj_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    org_id = whoami(token)["organizations"][0]["id"]
    return token, org_id


@pytest.mark.e2e
def test_public_report_endpoint_serves_report_inside_project(
    create_user, login_user, whoami, create_project, create_report, test_client,
):
    token, org_id = _owner(create_user, login_user, whoami)
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    project = create_project(name="Aviv", user_token=token, org_id=org_id)
    report = create_report(
        title="Dashboard in a project",
        user_token=token,
        org_id=org_id,
        project_id=project["id"],
    )

    resp = test_client.get(f"/api/r/{report['id']}", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == report["id"]
    assert body["project_id"] == project["id"]
    assert (body.get("project") or {}).get("id") == project["id"]


@pytest.mark.e2e
def test_public_report_endpoint_serves_report_without_project(
    create_user, login_user, whoami, create_report, test_client,
):
    """The no-project case must keep working (it always did)."""
    token, org_id = _owner(create_user, login_user, whoami)
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    report = create_report(title="Rootless report", user_token=token, org_id=org_id)

    resp = test_client.get(f"/api/r/{report['id']}", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["project"] is None
