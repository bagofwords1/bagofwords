"""
E2E tests for per-user report starring.

Covers:
- POST   /reports/{id}/star    (star_report)
- DELETE /reports/{id}/star    (unstar_report)
- is_starred field on GET /reports list
- Starred reports surface at the top of the list (ordering)
- Starring is per-user (independent across users)
- A user can star a report shared with them (read-only)
"""
import pytest
import uuid


def _setup_two_users(create_user, login_user, whoami, test_client):
    """Helper: create two users in the same org."""
    email1 = f"star_owner_{uuid.uuid4().hex[:6]}@test.com"
    user1 = create_user(email=email1, password="test123")
    token1 = login_user(email=email1, password="test123")
    me1 = whoami(token1)
    org_id = me1["organizations"][0]["id"]

    email2 = f"star_member_{uuid.uuid4().hex[:6]}@test.com"
    test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": email2, "role": "member"},
        headers={"Authorization": f"Bearer {token1}", "X-Organization-Id": org_id},
    )
    user2 = create_user(email=email2, password="test123")
    token2 = login_user(email=email2, password="test123")
    me2 = whoami(token2)
    user2_id = me2["id"]

    return token1, token2, org_id, user2_id


def _find(reports, report_id):
    for r in reports:
        if r["id"] == report_id:
            return r
    return None


@pytest.mark.e2e
def test_star_and_unstar_sets_is_starred(
    create_report, list_reports, star_report, unstar_report,
    create_user, login_user, whoami,
):
    """Starring a report toggles is_starred on the list view."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    report = create_report(title="To Star", user_token=token, org_id=org_id, data_sources=[])

    # Initially not starred
    listed = list_reports(user_token=token, org_id=org_id, filter="my")
    assert _find(listed["reports"], report["id"])["is_starred"] is False

    # Star it
    resp = star_report(report["id"], user_token=token, org_id=org_id)
    body = resp.json()
    assert body["id"] == report["id"]
    assert body["is_starred"] is True

    listed = list_reports(user_token=token, org_id=org_id, filter="my")
    assert _find(listed["reports"], report["id"])["is_starred"] is True

    # Unstar it
    resp = unstar_report(report["id"], user_token=token, org_id=org_id)
    assert resp.json()["is_starred"] is False

    listed = list_reports(user_token=token, org_id=org_id, filter="my")
    assert _find(listed["reports"], report["id"])["is_starred"] is False


@pytest.mark.e2e
def test_star_is_idempotent(
    create_report, list_reports, star_report,
    create_user, login_user, whoami,
):
    """Starring twice keeps the report starred exactly once (no duplicate rows)."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    report = create_report(title="Double Star", user_token=token, org_id=org_id, data_sources=[])

    star_report(report["id"], user_token=token, org_id=org_id)
    star_report(report["id"], user_token=token, org_id=org_id)

    listed = list_reports(user_token=token, org_id=org_id, filter="my")
    matches = [r for r in listed["reports"] if r["id"] == report["id"]]
    assert len(matches) == 1
    assert matches[0]["is_starred"] is True


@pytest.mark.e2e
def test_starred_reports_appear_first(
    create_report, list_reports, star_report,
    create_user, login_user, whoami,
):
    """A starred report is ordered ahead of newer non-starred reports."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    # Created oldest-first; default ordering is created_at desc.
    first = create_report(title="First", user_token=token, org_id=org_id, data_sources=[])
    create_report(title="Second", user_token=token, org_id=org_id, data_sources=[])
    create_report(title="Third", user_token=token, org_id=org_id, data_sources=[])

    # Without starring, the oldest report is last.
    listed = list_reports(user_token=token, org_id=org_id, filter="my")
    ids = [r["id"] for r in listed["reports"]]
    assert ids[0] != first["id"]

    # Star the oldest report -> it should jump to the top.
    star_report(first["id"], user_token=token, org_id=org_id)
    listed = list_reports(user_token=token, org_id=org_id, filter="my")
    assert listed["reports"][0]["id"] == first["id"]
    assert listed["reports"][0]["is_starred"] is True


@pytest.mark.e2e
def test_starring_is_per_user(
    test_client, create_report, list_reports, star_report,
    create_user, login_user, whoami,
):
    """User A starring a report does not star it for user B."""
    token1, token2, org_id, user2_id = _setup_two_users(
        create_user, login_user, whoami, test_client
    )

    # Owner makes a report visible to the org so user2 can see it.
    report = create_report(title="Shared Star", user_token=token1, org_id=org_id, data_sources=[])
    test_client.put(
        f"/api/reports/{report['id']}/visibility/artifact",
        json={"visibility": "internal"},
        headers={"Authorization": f"Bearer {token1}", "X-Organization-Id": org_id},
    )

    # Owner stars it
    star_report(report["id"], user_token=token1, org_id=org_id)

    # Owner sees it starred
    owner_list = list_reports(user_token=token1, org_id=org_id, filter="my")
    assert _find(owner_list["reports"], report["id"])["is_starred"] is True

    # User2 sees the same report (shared tab) but NOT starred for them
    member_list = list_reports(user_token=token2, org_id=org_id, filter="shared")
    member_view = _find(member_list["reports"], report["id"])
    assert member_view is not None
    assert member_view["is_starred"] is False


@pytest.mark.e2e
def test_user_can_star_report_shared_with_them(
    test_client, create_report, list_reports, star_report,
    create_user, login_user, whoami,
):
    """A non-owner can star a report shared with them read-only."""
    token1, token2, org_id, user2_id = _setup_two_users(
        create_user, login_user, whoami, test_client
    )

    report = create_report(title="Star As Viewer", user_token=token1, org_id=org_id, data_sources=[])
    test_client.put(
        f"/api/reports/{report['id']}/visibility/artifact",
        json={"visibility": "internal"},
        headers={"Authorization": f"Bearer {token1}", "X-Organization-Id": org_id},
    )

    # User2 (viewer) stars it successfully
    resp = star_report(report["id"], user_token=token2, org_id=org_id)
    assert resp.json()["is_starred"] is True

    member_list = list_reports(user_token=token2, org_id=org_id, filter="shared")
    assert _find(member_list["reports"], report["id"])["is_starred"] is True

    # Owner's star state is unaffected
    owner_list = list_reports(user_token=token1, org_id=org_id, filter="my")
    assert _find(owner_list["reports"], report["id"])["is_starred"] is False


@pytest.mark.e2e
def test_star_nonexistent_report_returns_404(
    star_report, create_user, login_user, whoami,
):
    """Starring a missing report returns 404."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    star_report(str(uuid.uuid4()), user_token=token, org_id=org_id, expect_status=404)
