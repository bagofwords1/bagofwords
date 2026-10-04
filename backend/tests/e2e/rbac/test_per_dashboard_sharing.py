"""Each dashboard of a report is shared on its own.

Invariants under test:
- Granting one dashboard opens that dashboard only: its versions are listed
  and readable on /r and /api/artifacts, every other dashboard of the same
  report stays closed to the viewer (list, detail, artifact API).
- A dashboard created after another one was shared starts private.
- Sharing follows the dashboard across edits (new versions keep it).
- Group grants work per dashboard; non-members of the group stay out.
- The whole-report endpoint still shares every dashboard at once, and a
  settings-only write (no visibility) leaves every dashboard's sharing alone.
- The report-level fields follow the most open dashboard, so listings agree,
  including when a dashboard is deleted.
- Deleting the last dashboard makes the report private again: its queries
  do not fall back open, and a dashboard rebuilt there starts private.
- Only the owner changes a dashboard's sharing, and only for its own report.
- Conversation sharing is a separate surface and is not opened by it.
"""
import uuid

import pytest


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _create_dashboard(test_client, *, report_id, token, org_id, title):
    resp = test_client.post(
        "/api/artifacts",
        json={
            "report_id": report_id,
            "title": title,
            "mode": "page",
            "content": {"code": f"<div>{title}</div>"},
        },
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _share_dashboard(test_client, *, report_id, artifact_id, token, org_id, visibility,
                     user_ids=None, group_ids=None, expect_status=200):
    body = {"visibility": visibility}
    if user_ids is not None:
        body["shared_user_ids"] = user_ids
    if group_ids is not None:
        body["shared_group_ids"] = group_ids
    resp = test_client.put(
        f"/api/reports/{report_id}/artifacts/{artifact_id}/visibility",
        json=body,
        headers=_headers(token, org_id),
    )
    assert resp.status_code == expect_status, resp.json()
    return resp.json()


def _visible_dashboards(test_client, report_id, headers=None):
    resp = test_client.get(f"/api/r/{report_id}/artifacts", headers=headers or {})
    assert resp.status_code == 200, resp.json()
    return {a["artifact_id"] for a in resp.json()}


@pytest.fixture
def two_dashboards(test_client, create_user, login_user, whoami, invite_user_to_org, create_report):
    """An owner's report holding two dashboards, plus two plain members."""
    owner = create_user()
    owner_token = login_user(owner["email"], owner["password"])
    org_id = whoami(owner_token)["organizations"][0]["id"]
    viewer = invite_user_to_org(org_id=org_id, admin_token=owner_token)
    other = invite_user_to_org(org_id=org_id, admin_token=owner_token)

    report = create_report(
        title=f"Two dashboards {uuid.uuid4().hex[:6]}",
        user_token=owner_token, org_id=org_id, data_sources=[],
    )
    sales = _create_dashboard(test_client, report_id=report["id"], token=owner_token, org_id=org_id, title="Sales")
    payroll = _create_dashboard(test_client, report_id=report["id"], token=owner_token, org_id=org_id, title="Payroll")
    return {
        "org_id": org_id,
        "owner_token": owner_token,
        "viewer": viewer,
        "other": other,
        "report": report,
        "sales": sales,
        "payroll": payroll,
    }


@pytest.mark.e2e
def test_sharing_one_dashboard_opens_only_that_dashboard(test_client, two_dashboards):
    c = two_dashboards
    rid = c["report"]["id"]
    sales, payroll = c["sales"], c["payroll"]
    _share_dashboard(test_client, report_id=rid, artifact_id=sales["artifact_id"],
                     token=c["owner_token"], org_id=c["org_id"], visibility="internal")
    viewer = _headers(c["viewer"]["token"], c["org_id"])

    assert _visible_dashboards(test_client, rid, viewer) == {sales["artifact_id"]}
    assert test_client.get(f"/api/r/{rid}", headers=viewer).status_code == 200
    assert test_client.get(f"/api/r/{rid}/artifacts/{sales['id']}", headers=viewer).status_code == 200
    assert test_client.get(f"/api/r/{rid}/artifacts/{payroll['id']}", headers=viewer).status_code == 404

    # The authenticated artifact API agrees with the share page.
    assert test_client.get(f"/api/artifacts/{sales['id']}", headers=viewer).status_code == 200
    assert test_client.get(f"/api/artifacts/{payroll['id']}", headers=viewer).status_code == 403
    listed = test_client.get(f"/api/artifacts/report/{rid}", headers=viewer)
    assert listed.status_code == 200, listed.json()
    assert {a["artifact_id"] for a in listed.json()} == {sales["artifact_id"]}

    # 'internal' still needs a sign-in; the owner still sees everything.
    assert test_client.get(f"/api/r/{rid}/artifacts/{sales['id']}").status_code == 401
    owner = _headers(c["owner_token"], c["org_id"])
    assert _visible_dashboards(test_client, rid, owner) == {sales["artifact_id"], payroll["artifact_id"]}


@pytest.mark.e2e
def test_dashboard_created_after_sharing_starts_private(test_client, two_dashboards):
    c = two_dashboards
    rid = c["report"]["id"]
    _share_dashboard(test_client, report_id=rid, artifact_id=c["sales"]["artifact_id"],
                     token=c["owner_token"], org_id=c["org_id"], visibility="public")
    _share_dashboard(test_client, report_id=rid, artifact_id=c["payroll"]["artifact_id"],
                     token=c["owner_token"], org_id=c["org_id"], visibility="public")

    later = _create_dashboard(test_client, report_id=rid, token=c["owner_token"], org_id=c["org_id"], title="Later")

    sharing = test_client.get(
        f"/api/reports/{rid}/artifacts/{later['artifact_id']}/sharing",
        headers=_headers(c["owner_token"], c["org_id"]),
    )
    assert sharing.status_code == 200, sharing.json()
    assert sharing.json()["visibility"] == "none"
    assert later["artifact_id"] not in _visible_dashboards(test_client, rid)
    assert test_client.get(f"/api/r/{rid}/artifacts/{later['id']}").status_code == 404


@pytest.mark.e2e
def test_new_version_keeps_the_dashboard_sharing(test_client, two_dashboards):
    c = two_dashboards
    rid = c["report"]["id"]
    sales = c["sales"]
    _share_dashboard(test_client, report_id=rid, artifact_id=sales["artifact_id"],
                     token=c["owner_token"], org_id=c["org_id"], visibility="shared",
                     user_ids=[c["viewer"]["user_id"]])

    dup = test_client.post(
        f"/api/artifacts/{sales['id']}/duplicate",
        headers=_headers(c["owner_token"], c["org_id"]),
    )
    assert dup.status_code == 200, dup.json()
    v2 = dup.json()
    assert v2["artifact_id"] == sales["artifact_id"] and v2["id"] != sales["id"]

    viewer = _headers(c["viewer"]["token"], c["org_id"])
    assert test_client.get(f"/api/r/{rid}/artifacts/{v2['id']}", headers=viewer).status_code == 200
    other = _headers(c["other"]["token"], c["org_id"])
    assert test_client.get(f"/api/r/{rid}/artifacts/{v2['id']}", headers=other).status_code == 403


@pytest.mark.e2e
def test_group_grant_opens_only_that_dashboard(
    test_client, enterprise_license, two_dashboards, create_group, add_user_to_group,
):
    c = two_dashboards
    rid = c["report"]["id"]
    group = create_group(name=f"Sales team {uuid.uuid4().hex[:6]}", user_token=c["owner_token"], org_id=c["org_id"])
    assert group.status_code == 200, group.json()
    group = group.json()
    added = add_user_to_group(group_id=group["id"], user_id=c["viewer"]["user_id"],
                              user_token=c["owner_token"], org_id=c["org_id"])
    assert added.status_code in (200, 201), added.json()

    _share_dashboard(test_client, report_id=rid, artifact_id=c["sales"]["artifact_id"],
                     token=c["owner_token"], org_id=c["org_id"], visibility="shared",
                     group_ids=[group["id"]])

    member = _headers(c["viewer"]["token"], c["org_id"])
    assert _visible_dashboards(test_client, rid, member) == {c["sales"]["artifact_id"]}
    assert test_client.get(f"/api/r/{rid}/artifacts/{c['payroll']['id']}", headers=member).status_code == 404
    outsider = _headers(c["other"]["token"], c["org_id"])
    assert test_client.get(f"/api/r/{rid}/artifacts/{c['sales']['id']}", headers=outsider).status_code == 403

    sharing = test_client.get(
        f"/api/reports/{rid}/artifacts/{c['sales']['artifact_id']}/sharing",
        headers=_headers(c["owner_token"], c["org_id"]),
    ).json()
    assert [(s["principal_type"], s["group_id"]) for s in sharing["shares"]] == [("group", group["id"])]


@pytest.mark.e2e
def test_whole_report_visibility_shares_every_dashboard(test_client, two_dashboards, set_visibility):
    c = two_dashboards
    rid = c["report"]["id"]
    set_visibility(report_id=rid, share_type="artifact", visibility="shared",
                   shared_user_ids=[c["viewer"]["user_id"]],
                   user_token=c["owner_token"], org_id=c["org_id"])

    viewer = _headers(c["viewer"]["token"], c["org_id"])
    assert _visible_dashboards(test_client, rid, viewer) == {c["sales"]["artifact_id"], c["payroll"]["artifact_id"]}
    owner = _headers(c["owner_token"], c["org_id"])
    for dash in (c["sales"], c["payroll"]):
        sharing = test_client.get(f"/api/reports/{rid}/artifacts/{dash['artifact_id']}/sharing", headers=owner).json()
        assert sharing["visibility"] == "shared"
        assert [s["user_id"] for s in sharing["shares"]] == [c["viewer"]["user_id"]]


@pytest.mark.e2e
def test_settings_only_write_leaves_dashboard_sharing_alone(test_client, two_dashboards):
    c = two_dashboards
    rid = c["report"]["id"]
    _share_dashboard(test_client, report_id=rid, artifact_id=c["sales"]["artifact_id"],
                     token=c["owner_token"], org_id=c["org_id"], visibility="internal")

    owner = _headers(c["owner_token"], c["org_id"])
    resp = test_client.put(f"/api/reports/{rid}/visibility/artifact", json={"include_data_tab": False}, headers=owner)
    assert resp.status_code == 200, resp.json()

    assert test_client.get(f"/api/reports/{rid}", headers=owner).json()["include_data_tab"] is False
    for dash, expected in ((c["sales"], "internal"), (c["payroll"], "none")):
        sharing = test_client.get(f"/api/reports/{rid}/artifacts/{dash['artifact_id']}/sharing", headers=owner).json()
        assert sharing["visibility"] == expected
    viewer = _headers(c["viewer"]["token"], c["org_id"])
    assert _visible_dashboards(test_client, rid, viewer) == {c["sales"]["artifact_id"]}

    # The conversation surface has no settings-only write.
    resp = test_client.put(f"/api/reports/{rid}/visibility/conversation", json={}, headers=owner)
    assert resp.status_code == 422


@pytest.mark.e2e
def test_report_fields_follow_the_most_open_dashboard(test_client, two_dashboards, list_reports):
    c = two_dashboards
    rid = c["report"]["id"]
    owner = _headers(c["owner_token"], c["org_id"])

    result = _share_dashboard(test_client, report_id=rid, artifact_id=c["payroll"]["artifact_id"],
                              token=c["owner_token"], org_id=c["org_id"], visibility="shared",
                              user_ids=[c["viewer"]["user_id"]])
    assert result["report_artifact_visibility"] == "shared"
    assert test_client.get(f"/api/reports/{rid}", headers=owner).json()["artifact_visibility"] == "shared"
    shared_ids = {r["id"] for r in list_reports(user_token=c["viewer"]["token"], org_id=c["org_id"], filter="shared")["reports"]}
    assert rid in shared_ids

    _share_dashboard(test_client, report_id=rid, artifact_id=c["sales"]["artifact_id"],
                     token=c["owner_token"], org_id=c["org_id"], visibility="public")
    assert test_client.get(f"/api/reports/{rid}", headers=owner).json()["artifact_visibility"] == "public"

    for dash in (c["sales"], c["payroll"]):
        _share_dashboard(test_client, report_id=rid, artifact_id=dash["artifact_id"],
                         token=c["owner_token"], org_id=c["org_id"], visibility="none")
    assert test_client.get(f"/api/reports/{rid}", headers=owner).json()["artifact_visibility"] == "none"
    shared_ids = {r["id"] for r in list_reports(user_token=c["viewer"]["token"], org_id=c["org_id"], filter="shared")["reports"]}
    assert rid not in shared_ids
    assert test_client.get(f"/api/r/{rid}", headers=_headers(c["viewer"]["token"], c["org_id"])).status_code == 404


@pytest.mark.e2e
def test_deleting_the_open_dashboard_closes_the_report(test_client, two_dashboards, list_reports):
    c = two_dashboards
    rid = c["report"]["id"]
    owner = _headers(c["owner_token"], c["org_id"])
    _share_dashboard(test_client, report_id=rid, artifact_id=c["sales"]["artifact_id"],
                     token=c["owner_token"], org_id=c["org_id"], visibility="internal")
    assert test_client.get(f"/api/reports/{rid}", headers=owner).json()["artifact_visibility"] == "internal"

    resp = test_client.delete(f"/api/artifacts/{c['sales']['id']}", headers=owner)
    assert resp.status_code == 200, resp.json()

    # Only the private dashboard is left: the report stops reading as shared.
    assert test_client.get(f"/api/reports/{rid}", headers=owner).json()["artifact_visibility"] == "none"
    viewer = _headers(c["viewer"]["token"], c["org_id"])
    assert test_client.get(f"/api/r/{rid}", headers=viewer).status_code == 404
    published = {r["id"] for r in list_reports(user_token=c["viewer"]["token"], org_id=c["org_id"], filter="published")["reports"]}
    assert rid not in published


@pytest.mark.e2e
def test_deleting_every_dashboard_makes_the_report_private(test_client, two_dashboards):
    c = two_dashboards
    rid = c["report"]["id"]
    owner = _headers(c["owner_token"], c["org_id"])
    viewer = _headers(c["viewer"]["token"], c["org_id"])
    for dash in (c["sales"], c["payroll"]):
        _share_dashboard(test_client, report_id=rid, artifact_id=dash["artifact_id"],
                         token=c["owner_token"], org_id=c["org_id"], visibility="shared",
                         user_ids=[c["viewer"]["user_id"]])
    for dash in (c["sales"], c["payroll"]):
        assert test_client.delete(f"/api/artifacts/{dash['id']}", headers=owner).status_code == 200

    # No dashboard left: the report-level gate must not stay open, or every
    # query of the conversation would be readable by the former grantees.
    assert test_client.get(f"/api/reports/{rid}", headers=owner).json()["artifact_visibility"] == "none"
    assert test_client.get(f"/api/r/{rid}", headers=viewer).status_code == 404
    assert test_client.get(f"/api/r/{rid}/queries", headers=viewer).status_code == 404

    rebuilt = _create_dashboard(test_client, report_id=rid, token=c["owner_token"], org_id=c["org_id"], title="Rebuilt")
    assert test_client.get(f"/api/r/{rid}/artifacts/{rebuilt['id']}", headers=viewer).status_code == 404


@pytest.mark.e2e
def test_only_the_owner_shares_a_dashboard_of_their_own_report(test_client, two_dashboards, create_report):
    c = two_dashboards
    rid = c["report"]["id"]
    _share_dashboard(test_client, report_id=rid, artifact_id=c["sales"]["artifact_id"],
                     token=c["viewer"]["token"], org_id=c["org_id"], visibility="public", expect_status=403)
    resp = test_client.get(f"/api/reports/{rid}/artifacts/{c['sales']['artifact_id']}/sharing",
                           headers=_headers(c["viewer"]["token"], c["org_id"]))
    assert resp.status_code == 403

    # A dashboard of another report can't be shared through this one.
    other_report = create_report(title="Other", user_token=c["owner_token"], org_id=c["org_id"], data_sources=[])
    _share_dashboard(test_client, report_id=other_report["id"], artifact_id=c["sales"]["artifact_id"],
                     token=c["owner_token"], org_id=c["org_id"], visibility="public", expect_status=404)


@pytest.mark.e2e
def test_conversation_share_does_not_open_dashboards(test_client, two_dashboards, set_visibility):
    c = two_dashboards
    rid = c["report"]["id"]
    shared = set_visibility(report_id=rid, share_type="conversation", visibility="public",
                            user_token=c["owner_token"], org_id=c["org_id"])
    token = shared.json()["conversation_share_token"]
    assert token

    assert test_client.get(f"/api/c/{token}").status_code == 200
    assert test_client.get(f"/api/r/{rid}").status_code == 404
    assert test_client.get(f"/api/r/{rid}/artifacts/{c['sales']['id']}").status_code == 404
