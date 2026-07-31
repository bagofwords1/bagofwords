"""
E2E tests for sharing a report's artifact/conversation with a group.

Group members get the same access a direct per-user share grants, resolved
through group membership at read time:
- PUT /reports/{id}/visibility/{type} accepts shared_group_ids
- GET /reports/{id}/shares/{type} lists group entries (principal_type=group)
- /r/{id} honors group membership; non-members stay blocked
- GET /reports?filter=shared surfaces group-shared reports
- GET /organization/groups exposes the share-picker group directory
"""
import uuid

import pytest


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def group_share_cast(
    enterprise_license, test_client, create_user, login_user, whoami,
    invite_user_to_org, create_group, add_user_to_group,
):
    """Admin org with a group containing one member and one outsider member."""
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]

    member = invite_user_to_org(org_id=org_id, admin_token=admin_token)
    outsider = invite_user_to_org(org_id=org_id, admin_token=admin_token)

    group_resp = create_group(
        name=f"Analysts {uuid.uuid4().hex[:6]}", user_token=admin_token, org_id=org_id
    )
    assert group_resp.status_code == 200, group_resp.json()
    group = group_resp.json()

    add_resp = add_user_to_group(
        group_id=group["id"], user_id=member["user_id"],
        user_token=admin_token, org_id=org_id,
    )
    assert add_resp.status_code in (200, 201), add_resp.json()

    return {
        "admin_token": admin_token,
        "org_id": org_id,
        "group": group,
        "member": member,
        "outsider": outsider,
    }


@pytest.mark.e2e
def test_share_with_group_lists_group_entry(
    group_share_cast, create_report, set_visibility, get_shares, get_report,
):
    cast = group_share_cast
    report = create_report(
        title="Group Shared", user_token=cast["admin_token"],
        org_id=cast["org_id"], data_sources=[],
    )

    resp = set_visibility(
        report["id"], "artifact", "shared",
        user_token=cast["admin_token"], org_id=cast["org_id"],
        shared_user_ids=[], shared_group_ids=[cast["group"]["id"]],
    )
    data = resp.json()
    assert data["visibility"] == "shared"
    assert cast["group"]["id"] in data["shared_group_ids"]

    shares = get_shares(report["id"], "artifact", user_token=cast["admin_token"], org_id=cast["org_id"])
    assert len(shares) == 1
    entry = shares[0]
    assert entry["principal_type"] == "group"
    assert entry["group_id"] == cast["group"]["id"]
    assert entry["group_name"] == cast["group"]["name"]
    assert entry["member_count"] == 1
    assert entry["user_id"] is None

    fetched = get_report(report["id"], user_token=cast["admin_token"], org_id=cast["org_id"])
    assert fetched["artifact_shared_group_ids"] == [cast["group"]["id"]]
    assert fetched["artifact_shared_user_ids"] == []


@pytest.mark.e2e
def test_group_member_can_view_shared_artifact(
    group_share_cast, test_client, create_report, set_visibility,
):
    cast = group_share_cast
    report = create_report(
        title="Group Access", user_token=cast["admin_token"],
        org_id=cast["org_id"], data_sources=[],
    )
    set_visibility(
        report["id"], "artifact", "shared",
        user_token=cast["admin_token"], org_id=cast["org_id"],
        shared_user_ids=[], shared_group_ids=[cast["group"]["id"]],
    )

    # Group member can view
    resp = test_client.get(
        f"/api/r/{report['id']}",
        headers={"Authorization": f"Bearer {cast['member']['token']}"},
    )
    assert resp.status_code == 200

    # Org member outside the group is blocked
    resp = test_client.get(
        f"/api/r/{report['id']}",
        headers={"Authorization": f"Bearer {cast['outsider']['token']}"},
    )
    assert resp.status_code == 403


@pytest.mark.e2e
def test_group_shared_report_appears_in_shared_list(
    group_share_cast, test_client, create_report, set_visibility,
):
    cast = group_share_cast
    report = create_report(
        title="Group List Entry", user_token=cast["admin_token"],
        org_id=cast["org_id"], data_sources=[],
    )
    set_visibility(
        report["id"], "artifact", "shared",
        user_token=cast["admin_token"], org_id=cast["org_id"],
        shared_user_ids=[], shared_group_ids=[cast["group"]["id"]],
    )

    def _shared_ids(token):
        resp = test_client.get(
            "/api/reports?filter=shared",
            headers=_headers(token, cast["org_id"]),
        )
        assert resp.status_code == 200, resp.json()
        body = resp.json()
        reports = body["reports"] if isinstance(body, dict) and "reports" in body else body
        return {r["id"] for r in reports}

    assert report["id"] in _shared_ids(cast["member"]["token"])
    assert report["id"] not in _shared_ids(cast["outsider"]["token"])


@pytest.mark.e2e
def test_remove_group_share_revokes_access(
    group_share_cast, test_client, create_report, set_visibility, get_shares,
):
    cast = group_share_cast
    report = create_report(
        title="Group Revoke", user_token=cast["admin_token"],
        org_id=cast["org_id"], data_sources=[],
    )
    set_visibility(
        report["id"], "artifact", "shared",
        user_token=cast["admin_token"], org_id=cast["org_id"],
        shared_user_ids=[], shared_group_ids=[cast["group"]["id"]],
    )

    # Remove the group by re-setting with an empty group list
    set_visibility(
        report["id"], "artifact", "shared",
        user_token=cast["admin_token"], org_id=cast["org_id"],
        shared_group_ids=[],
    )
    shares = get_shares(report["id"], "artifact", user_token=cast["admin_token"], org_id=cast["org_id"])
    assert shares == []

    resp = test_client.get(
        f"/api/r/{report['id']}",
        headers={"Authorization": f"Bearer {cast['member']['token']}"},
    )
    assert resp.status_code == 403


@pytest.mark.e2e
def test_user_shares_survive_group_only_update(
    group_share_cast, create_report, set_visibility, get_shares,
):
    """Omitting shared_user_ids while updating groups leaves user grants intact."""
    cast = group_share_cast
    report = create_report(
        title="Independent Lists", user_token=cast["admin_token"],
        org_id=cast["org_id"], data_sources=[],
    )
    set_visibility(
        report["id"], "artifact", "shared",
        user_token=cast["admin_token"], org_id=cast["org_id"],
        shared_user_ids=[cast["outsider"]["user_id"]],
    )
    set_visibility(
        report["id"], "artifact", "shared",
        user_token=cast["admin_token"], org_id=cast["org_id"],
        shared_group_ids=[cast["group"]["id"]],
    )

    shares = get_shares(report["id"], "artifact", user_token=cast["admin_token"], org_id=cast["org_id"])
    types = sorted(s["principal_type"] for s in shares)
    assert types == ["group", "user"]


@pytest.mark.e2e
def test_share_with_foreign_group_rejected(
    group_share_cast, create_user, login_user, whoami, create_report, set_visibility,
):
    """A group from another organization cannot be granted access."""
    cast = group_share_cast
    report = create_report(
        title="Foreign Group", user_token=cast["admin_token"],
        org_id=cast["org_id"], data_sources=[],
    )

    resp = set_visibility(
        report["id"], "artifact", "shared",
        user_token=cast["admin_token"], org_id=cast["org_id"],
        shared_group_ids=[str(uuid.uuid4())],
        expect_status=400,
    )
    assert resp.status_code == 400


@pytest.mark.e2e
def test_organization_groups_directory(group_share_cast, test_client):
    """Any org member can list groups (id, name, member_count) for the picker."""
    cast = group_share_cast
    resp = test_client.get(
        "/api/organization/groups",
        headers=_headers(cast["member"]["token"], cast["org_id"]),
    )
    assert resp.status_code == 200, resp.json()
    groups = resp.json()
    entry = next((g for g in groups if g["id"] == cast["group"]["id"]), None)
    assert entry is not None
    assert entry["name"] == cast["group"]["name"]
    assert entry["member_count"] == 1
