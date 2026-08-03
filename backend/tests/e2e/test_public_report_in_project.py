"""Regression: GET /api/r/{id} for a report that lives inside a project.

The published-dashboard page (/r/{id}) is what every dashboard card links
to — the project page, /dashboards, and the home page all render
RecentReportCard in 'org' view mode. ``Report.project`` is a
``lazy="joined"`` to-one, and the public loader turns eager loading off
wholesale with ``lazyload("*")`` — so serializing ``ReportSchema.project``
used to lazy-load on an async session and blow up (MissingGreenlet -> 500),
but only for reports that actually carry a ``project_id``. The frontend maps
any non-401/403 failure to /not_found, which is why the card read as a 404.
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
def test_public_report_endpoint_serves_project_collaborator(
    create_user, login_user, whoami, create_project, create_report,
    upsert_project_member, test_client,
):
    """A read-only collaborator reaches the same card from /dashboards.

    Their access comes from project membership, not from artifact_visibility,
    so they take the same serialization path as the owner.
    """
    owner_token, org_id = _owner(create_user, login_user, whoami)

    member_email = f"pub_proj_m_{uuid.uuid4().hex[:6]}@test.com"
    test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": member_email, "role": "member"},
        headers={"Authorization": f"Bearer {owner_token}", "X-Organization-Id": str(org_id)},
    )
    create_user(email=member_email, password="test123")
    member_token = login_user(email=member_email, password="test123")
    member_id = whoami(member_token)["id"]

    project = create_project(name="Aviv", user_token=owner_token, org_id=org_id)
    upsert_project_member(
        project["id"], member_id, permissions=["view"],
        user_token=owner_token, org_id=org_id,
    )
    report = create_report(
        title="Shared dashboard",
        user_token=owner_token,
        org_id=org_id,
        project_id=project["id"],
    )

    resp = test_client.get(
        f"/api/r/{report['id']}",
        headers={"Authorization": f"Bearer {member_token}", "X-Organization-Id": str(org_id)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["project_id"] == project["id"]


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
