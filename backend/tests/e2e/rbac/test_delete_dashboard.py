"""Deleting one dashboard of a report: DELETE /reports/{rid}/artifacts/{aid}.

Invariants under test:
- Every version of the dashboard goes, not just the latest; the report and
  its other dashboards stay.
- A deleted dashboard is no longer served to viewers it was shared with.
- Deleting the last dashboard puts the report back to private.
- Only the owner deletes; a member's attempt changes nothing.
- A dashboard is addressed within its own report: another report's id, an
  unknown id or an already deleted dashboard is not found.
"""
import uuid

import pytest


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _create_dashboard(test_client, *, report_id, token, org_id, title):
    resp = test_client.post(
        "/api/artifacts",
        json={"report_id": report_id, "title": title, "mode": "page",
              "content": {"code": f"<div>{title}</div>"}},
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _delete(test_client, report_id, artifact_id, headers):
    return test_client.delete(f"/api/reports/{report_id}/artifacts/{artifact_id}", headers=headers)


def _share(test_client, report_id, artifact_id, headers, visibility="internal"):
    resp = test_client.put(
        f"/api/reports/{report_id}/artifacts/{artifact_id}/visibility",
        json={"visibility": visibility}, headers=headers,
    )
    assert resp.status_code == 200, resp.json()


def _owner_versions(test_client, report_id, headers):
    """{dashboard id: {version ids}} as the owner's report page lists them."""
    resp = test_client.get(f"/api/artifacts/report/{report_id}", headers=headers)
    assert resp.status_code == 200, resp.json()
    out: dict[str, set[str]] = {}
    for a in resp.json():
        out.setdefault(a["artifact_id"], set()).add(a["id"])
    return out


@pytest.fixture
def owner_setup(test_client, create_user, login_user, whoami, invite_user_to_org, create_report):
    """An owner's report with two dashboards (Sales has two versions), plus a
    plain member of the org."""
    owner = create_user()
    token = login_user(owner["email"], owner["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    member = invite_user_to_org(org_id=org_id, admin_token=token)
    report = create_report(title=f"Delete {uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id, data_sources=[])
    headers = _headers(token, org_id)
    sales = _create_dashboard(test_client, report_id=report["id"], token=token, org_id=org_id, title="Sales")
    resp = test_client.post(f"/api/artifacts/{sales['id']}/duplicate", headers=headers)
    assert resp.status_code == 200, resp.json()
    payroll = _create_dashboard(test_client, report_id=report["id"], token=token, org_id=org_id, title="Payroll")
    return {"headers": headers, "org_id": org_id, "member": member, "report": report,
            "sales": sales, "payroll": payroll}


@pytest.mark.e2e
def test_owner_deletes_every_version_and_keeps_the_rest(test_client, owner_setup):
    c = owner_setup
    rid, owner = c["report"]["id"], c["headers"]
    before = _owner_versions(test_client, rid, owner)
    assert len(before[c["sales"]["artifact_id"]]) >= 2  # the setup really has several versions

    resp = _delete(test_client, rid, c["sales"]["artifact_id"], owner)
    assert resp.status_code == 200, resp.json()

    after = _owner_versions(test_client, rid, owner)
    assert set(after) == {c["payroll"]["artifact_id"]}
    for version_id in before[c["sales"]["artifact_id"]]:
        assert test_client.get(f"/api/artifacts/{version_id}", headers=owner).status_code == 404
    assert test_client.get(f"/api/reports/{rid}", headers=owner).status_code == 200


@pytest.mark.e2e
def test_deleted_dashboard_is_no_longer_served_to_viewers(test_client, owner_setup):
    c = owner_setup
    rid, owner = c["report"]["id"], c["headers"]
    _share(test_client, rid, c["sales"]["artifact_id"], owner)
    _share(test_client, rid, c["payroll"]["artifact_id"], owner)
    member = _headers(c["member"]["token"], c["org_id"])

    assert _delete(test_client, rid, c["sales"]["artifact_id"], owner).status_code == 200

    visible = {a["artifact_id"] for a in test_client.get(f"/api/r/{rid}/artifacts", headers=member).json()}
    assert visible == {c["payroll"]["artifact_id"]}
    assert test_client.get(f"/api/r/{rid}/artifacts/{c['sales']['id']}", headers=member).status_code == 404


@pytest.mark.e2e
def test_deleting_the_last_dashboard_makes_the_report_private(test_client, owner_setup):
    c = owner_setup
    rid, owner = c["report"]["id"], c["headers"]
    _share(test_client, rid, c["sales"]["artifact_id"], owner, visibility="public")
    assert test_client.get(f"/api/reports/{rid}", headers=owner).json()["artifact_visibility"] == "public"

    for key in ("sales", "payroll"):
        assert _delete(test_client, rid, c[key]["artifact_id"], owner).status_code == 200

    assert test_client.get(f"/api/reports/{rid}", headers=owner).json()["artifact_visibility"] == "none"


@pytest.mark.e2e
def test_only_the_owner_deletes(test_client, owner_setup):
    c = owner_setup
    rid = c["report"]["id"]
    _share(test_client, rid, c["sales"]["artifact_id"], c["headers"])
    member = _headers(c["member"]["token"], c["org_id"])

    resp = _delete(test_client, rid, c["sales"]["artifact_id"], member)
    assert resp.status_code == 403, resp.json()
    assert c["sales"]["artifact_id"] in _owner_versions(test_client, rid, c["headers"])


@pytest.mark.e2e
def test_dashboard_is_addressed_within_its_report(test_client, owner_setup, create_report):
    c = owner_setup
    rid, owner = c["report"]["id"], c["headers"]
    other = create_report(title="Other", user_token=owner["Authorization"].split()[1],
                          org_id=c["org_id"], data_sources=[])

    assert _delete(test_client, other["id"], c["sales"]["artifact_id"], owner).status_code == 404
    assert _delete(test_client, rid, str(uuid.uuid4()), owner).status_code == 404
    assert c["sales"]["artifact_id"] in _owner_versions(test_client, rid, owner)

    assert _delete(test_client, rid, c["sales"]["artifact_id"], owner).status_code == 200
    assert _delete(test_client, rid, c["sales"]["artifact_id"], owner).status_code == 404
