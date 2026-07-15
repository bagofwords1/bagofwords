"""Org admins (full_admin_access) can VIEW any report and artifact in their org.

Invariants under test:
- An org admin may open any report/artifact in the org directly by ID, even
  when its visibility is 'none' (private) and the admin is not the owner.
- The bypass is VIEW-only: ownership still gates mutations (update/delete)
  for admins exactly as before.
- Artifact routes enforce object-level access at all (they previously checked
  nothing): access follows the parent report's artifact visibility, so a
  non-owner member is denied on a private report's artifact and cannot
  mutate it, while shared/internal viewers keep read access.
- Admins of OTHER orgs never see the object at all (org scoping intact).

Feedback loop: docs/feedback-loops/admin-view-private-reports.md
"""
import uuid

import pytest


def _headers(token: str, org_id: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": str(org_id),
    }


def _create_private_report_with_artifact(test_client, create_report, owner_token, org_id):
    """Owner creates a report (default visibility 'none') plus one artifact."""
    report = create_report(
        title=f"Private {uuid.uuid4().hex[:6]}",
        user_token=owner_token,
        org_id=org_id,
        data_sources=[],
    )
    assert report["artifact_visibility"] == "none"

    resp = test_client.post(
        "/api/artifacts",
        json={
            "report_id": report["id"],
            "title": "Owner dashboard",
            "mode": "page",
            "content": {"code": "<div>owner-only dashboard</div>"},
        },
        headers=_headers(owner_token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    return report, resp.json()


@pytest.mark.e2e
def test_org_admin_can_view_any_private_report_and_artifact(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    admin = bootstrap_admin()
    owner = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    report, artifact = _create_private_report_with_artifact(
        test_client, create_report, owner["token"], admin["org_id"]
    )
    headers = _headers(admin["token"], admin["org_id"])

    # Report detail
    resp = test_client.get(f"/api/reports/{report['id']}", headers=headers)
    assert resp.status_code == 200, resp.json()
    assert resp.json()["id"] == report["id"]

    # Artifact detail + per-report artifact listing
    resp = test_client.get(f"/api/artifacts/{artifact['id']}", headers=headers)
    assert resp.status_code == 200, resp.json()
    assert resp.json()["id"] == artifact["id"]

    resp = test_client.get(f"/api/artifacts/report/{report['id']}", headers=headers)
    assert resp.status_code == 200, resp.json()
    assert {a["id"] for a in resp.json()} == {artifact["id"]}

    # Owner-gated read companions of the report page
    resp = test_client.get(f"/api/reports/{report['id']}/notes", headers=headers)
    assert resp.status_code == 200, resp.json()


@pytest.mark.e2e
def test_non_admin_member_still_denied_private_report_and_artifact(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    admin = bootstrap_admin()
    owner = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    bystander = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    report, artifact = _create_private_report_with_artifact(
        test_client, create_report, owner["token"], admin["org_id"]
    )
    headers = _headers(bystander["token"], admin["org_id"])

    resp = test_client.get(f"/api/reports/{report['id']}", headers=headers)
    assert resp.status_code == 403, resp.json()

    resp = test_client.get(f"/api/artifacts/{artifact['id']}", headers=headers)
    assert resp.status_code == 403, resp.json()

    # A non-owner member must not be able to mutate someone else's artifact
    resp = test_client.patch(
        f"/api/artifacts/{artifact['id']}",
        json={"title": "hijacked"},
        headers=headers,
    )
    assert resp.status_code == 403, resp.json()

    resp = test_client.delete(f"/api/artifacts/{artifact['id']}", headers=headers)
    assert resp.status_code == 403, resp.json()


@pytest.mark.e2e
def test_shared_viewer_keeps_artifact_read_access(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """Artifact reads follow the parent report's visibility, so tightening the
    artifact routes must not lock out legitimately-shared viewers."""
    admin = bootstrap_admin()
    owner = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    viewer = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    report, artifact = _create_private_report_with_artifact(
        test_client, create_report, owner["token"], admin["org_id"]
    )

    resp = test_client.put(
        f"/api/reports/{report['id']}/visibility/artifact",
        json={"visibility": "internal"},
        headers=_headers(owner["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.json()

    headers = _headers(viewer["token"], admin["org_id"])
    resp = test_client.get(f"/api/artifacts/{artifact['id']}", headers=headers)
    assert resp.status_code == 200, resp.json()

    resp = test_client.get(f"/api/artifacts/report/{report['id']}", headers=headers)
    assert resp.status_code == 200, resp.json()

    # Visibility grants reads, never writes
    resp = test_client.patch(
        f"/api/artifacts/{artifact['id']}",
        json={"title": "hijacked"},
        headers=headers,
    )
    assert resp.status_code == 403, resp.json()


@pytest.mark.e2e
def test_admin_view_bypass_does_not_grant_mutation(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    admin = bootstrap_admin()
    owner = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    report, artifact = _create_private_report_with_artifact(
        test_client, create_report, owner["token"], admin["org_id"]
    )
    headers = _headers(admin["token"], admin["org_id"])

    # Update / delete another user's report stays owner-only
    resp = test_client.put(
        f"/api/reports/{report['id']}",
        json={"title": "hijacked"},
        headers=headers,
    )
    assert resp.status_code == 403, resp.json()

    resp = test_client.delete(f"/api/reports/{report['id']}", headers=headers)
    assert resp.status_code == 403, resp.json()

    # Same for another user's artifact
    resp = test_client.patch(
        f"/api/artifacts/{artifact['id']}",
        json={"title": "hijacked"},
        headers=headers,
    )
    assert resp.status_code == 403, resp.json()

    resp = test_client.delete(f"/api/artifacts/{artifact['id']}", headers=headers)
    assert resp.status_code == 403, resp.json()


@pytest.mark.e2e
def test_admin_of_another_org_cannot_view(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    admin = bootstrap_admin()
    owner = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    report, artifact = _create_private_report_with_artifact(
        test_client, create_report, owner["token"], admin["org_id"]
    )

    outsider = bootstrap_admin("outsider")
    headers = _headers(outsider["token"], outsider["org_id"])

    resp = test_client.get(f"/api/reports/{report['id']}", headers=headers)
    assert resp.status_code == 404, resp.json()

    resp = test_client.get(f"/api/artifacts/{artifact['id']}", headers=headers)
    assert resp.status_code == 404, resp.json()

    resp = test_client.get(f"/api/artifacts/report/{report['id']}", headers=headers)
    assert resp.status_code == 404, resp.json()
