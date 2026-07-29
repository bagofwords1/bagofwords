"""
E2E tests for Projects (private-by-default shared folders for reports).

Covers:
- POST/GET/PUT/DELETE /projects (CRUD + field validation)
- Access model: private by default; owner-only; grants via
  PUT/DELETE /projects/{id}/members; access='org' visibility
- GET /reports?project_id=... filtering ('none' = root list)
- Moving reports via PUT /reports/{id} {project_id} and POST /reports/bulk/move
- Deleting a project returns its reports to the owners' root lists
- Reports inside a visible project surface in the non-owner's visible lists
"""
import pytest
import uuid

from app.settings.config import settings as bow_settings


@pytest.fixture
def _allow_multiple_orgs():
    """Allow one user to own several orgs — needed for the org-scoping test."""
    flags = bow_settings.bow_config.features
    saved = flags.allow_multiple_organizations
    flags.allow_multiple_organizations = True
    try:
        yield
    finally:
        flags.allow_multiple_organizations = saved


def _setup_two_users(create_user, login_user, whoami, test_client):
    """Two users in the same org: (owner_token, member_token, org_id, member_user_id)."""
    email1 = f"proj_owner_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email1, password="test123")
    token1 = login_user(email=email1, password="test123")
    org_id = whoami(token1)["organizations"][0]["id"]

    email2 = f"proj_member_{uuid.uuid4().hex[:6]}@test.com"
    test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": email2, "role": "member"},
        headers={"Authorization": f"Bearer {token1}", "X-Organization-Id": org_id},
    )
    create_user(email=email2, password="test123")
    token2 = login_user(email=email2, password="test123")
    member_user_id = whoami(token2)["id"]
    return token1, token2, org_id, member_user_id


def _project_ids(listing):
    return {p["id"] for p in listing["projects"]}


# ── CRUD ─────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_project_crud_roundtrip(
    create_project, list_projects, get_project, update_project, delete_project,
    create_user, login_user, whoami,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    project = create_project(name="Marketing", user_token=token, org_id=org_id,
                             description="Q3 analyses", color="#2563eb")
    assert project["name"] == "Marketing"
    assert project["access"] == "private"
    assert project["is_owner"] is True
    assert project["can_manage"] is True
    assert project["report_count"] == 0

    listed = list_projects(user_token=token, org_id=org_id)
    assert project["id"] in _project_ids(listed)

    fetched = get_project(project["id"], user_token=token, org_id=org_id).json()
    assert fetched["description"] == "Q3 analyses"
    assert fetched["color"] == "#2563eb"

    updated = update_project(project["id"], user_token=token, org_id=org_id,
                             name="Marketing 2.0", color="#16a34a").json()
    assert updated["name"] == "Marketing 2.0"
    assert updated["color"] == "#16a34a"

    delete_project(project["id"], user_token=token, org_id=org_id)
    listed = list_projects(user_token=token, org_id=org_id)
    assert project["id"] not in _project_ids(listed)
    get_project(project["id"], user_token=token, org_id=org_id, expect_status=404)


@pytest.mark.e2e
def test_project_name_validation(create_project, create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    resp = create_project(name="   ", user_token=token, org_id=org_id, expect_status=422)
    assert resp.status_code == 422
    resp = create_project(name="x" * 200, user_token=token, org_id=org_id, expect_status=422)
    assert resp.status_code == 422


# ── Access model ─────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_private_project_is_invisible_to_other_members(
    test_client, create_project, list_projects, get_project,
    create_user, login_user, whoami,
):
    token1, token2, org_id, _ = _setup_two_users(create_user, login_user, whoami, test_client)

    project = create_project(name="Owner Only", user_token=token1, org_id=org_id)

    # Not in the other member's list, and direct GET is a 404 (no existence leak).
    listed = list_projects(user_token=token2, org_id=org_id)
    assert project["id"] not in _project_ids(listed)
    get_project(project["id"], user_token=token2, org_id=org_id, expect_status=404)


@pytest.mark.e2e
def test_granted_member_can_view_but_not_manage(
    test_client, create_project, list_projects, get_project, update_project,
    delete_project, upsert_project_member, remove_project_member,
    create_user, login_user, whoami,
):
    token1, token2, org_id, member_id = _setup_two_users(create_user, login_user, whoami, test_client)

    project = create_project(name="Shared Folder", user_token=token1, org_id=org_id)
    upsert_project_member(project["id"], member_id, permissions=["view"],
                          user_token=token1, org_id=org_id)

    # Member now sees it, with can_manage=False.
    listed = list_projects(user_token=token2, org_id=org_id)
    assert project["id"] in _project_ids(listed)
    fetched = get_project(project["id"], user_token=token2, org_id=org_id).json()
    assert fetched["is_owner"] is False
    assert fetched["can_manage"] is False

    # View grant does not allow rename, membership changes, or delete.
    update_project(project["id"], user_token=token2, org_id=org_id,
                   name="Hijacked", expect_status=403)
    upsert_project_member(project["id"], member_id, permissions=["manage"],
                          user_token=token2, org_id=org_id, expect_status=403)
    delete_project(project["id"], user_token=token2, org_id=org_id, expect_status=403)

    # Revoking removes visibility again.
    remove_project_member(project["id"], member_id, user_token=token1, org_id=org_id)
    listed = list_projects(user_token=token2, org_id=org_id)
    assert project["id"] not in _project_ids(listed)

    # Re-adding a previously removed member must succeed (the soft-deleted
    # grant row is resurrected, not duplicated) and restore visibility.
    upsert_project_member(project["id"], member_id, permissions=["view"],
                          user_token=token1, org_id=org_id)
    listed = list_projects(user_token=token2, org_id=org_id)
    assert project["id"] in _project_ids(listed)


@pytest.mark.e2e
def test_manage_grant_allows_rename_but_not_delete(
    test_client, create_project, update_project, delete_project, upsert_project_member,
    create_user, login_user, whoami,
):
    token1, token2, org_id, member_id = _setup_two_users(create_user, login_user, whoami, test_client)

    project = create_project(name="Managed", user_token=token1, org_id=org_id)
    upsert_project_member(project["id"], member_id, permissions=["manage"],
                          user_token=token1, org_id=org_id)

    renamed = update_project(project["id"], user_token=token2, org_id=org_id,
                             name="Managed v2").json()
    assert renamed["name"] == "Managed v2"
    assert renamed["can_manage"] is True

    # Delete stays owner-only.
    delete_project(project["id"], user_token=token2, org_id=org_id, expect_status=403)


@pytest.mark.e2e
def test_org_access_project_visible_to_all_members(
    test_client, create_project, update_project, list_projects, get_project,
    create_user, login_user, whoami,
):
    token1, token2, org_id, _ = _setup_two_users(create_user, login_user, whoami, test_client)

    project = create_project(name="Org Wide", user_token=token1, org_id=org_id)
    update_project(project["id"], user_token=token1, org_id=org_id, access="org")

    listed = list_projects(user_token=token2, org_id=org_id)
    assert project["id"] in _project_ids(listed)
    fetched = get_project(project["id"], user_token=token2, org_id=org_id).json()
    assert fetched["can_manage"] is False


@pytest.mark.e2e
def test_member_endpoints_reject_non_org_users_and_owner(
    test_client, create_project, upsert_project_member,
    create_user, login_user, whoami,
):
    token1, _, org_id, _ = _setup_two_users(create_user, login_user, whoami, test_client)
    project = create_project(name="Members", user_token=token1, org_id=org_id)
    owner_id = whoami(token1)["id"]

    # Owner can't be granted (already implicit).
    upsert_project_member(project["id"], owner_id, user_token=token1, org_id=org_id,
                          expect_status=400)

    # A principal that is not a member of this org can't be granted.
    upsert_project_member(project["id"], str(uuid.uuid4()), user_token=token1,
                          org_id=org_id, expect_status=400)


# ── Report membership & filtering ────────────────────────────────────────────

@pytest.mark.e2e
def test_move_report_into_and_out_of_project(
    create_project, create_report, list_reports, move_report_to_project,
    create_user, login_user, whoami,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    project = create_project(name="Folder", user_token=token, org_id=org_id)
    report = create_report(title="Inside", user_token=token, org_id=org_id, data_sources=[])
    other = create_report(title="Outside", user_token=token, org_id=org_id, data_sources=[])

    moved = move_report_to_project(report["id"], project["id"],
                                   user_token=token, org_id=org_id).json()
    assert moved["project_id"] == project["id"]
    assert moved["project"]["name"] == "Folder"

    # Filter to the project: only the moved report.
    in_project = list_reports(user_token=token, org_id=org_id,
                              filter="all", project_id=project["id"])
    ids = {r["id"] for r in in_project["reports"]}
    assert ids == {report["id"]}

    # Root list ('none') excludes it but keeps the other one.
    root = list_reports(user_token=token, org_id=org_id, filter="my", project_id="none")
    root_ids = {r["id"] for r in root["reports"]}
    assert report["id"] not in root_ids
    assert other["id"] in root_ids

    # Unfiltered list still contains both (project membership isn't hiding).
    all_mine = list_reports(user_token=token, org_id=org_id, filter="my")
    all_ids = {r["id"] for r in all_mine["reports"]}
    assert {report["id"], other["id"]} <= all_ids

    # Move back out.
    cleared = move_report_to_project(report["id"], None,
                                     user_token=token, org_id=org_id).json()
    assert cleared["project_id"] is None
    root = list_reports(user_token=token, org_id=org_id, filter="my", project_id="none")
    assert report["id"] in {r["id"] for r in root["reports"]}


@pytest.mark.e2e
def test_create_report_directly_in_project(
    test_client, create_project, list_reports, create_user, login_user, whoami,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    project = create_project(name="Direct", user_token=token, org_id=org_id)
    resp = test_client.post(
        "/api/reports",
        json={"title": "born inside", "files": [], "data_sources": [],
              "project_id": project["id"]},
        headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["project_id"] == project["id"]

    in_project = list_reports(user_token=token, org_id=org_id,
                              filter="all", project_id=project["id"])
    assert resp.json()["id"] in {r["id"] for r in in_project["reports"]}


@pytest.mark.e2e
def test_bulk_move_reports(
    test_client, create_project, create_report, list_reports,
    create_user, login_user, whoami,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    project = create_project(name="Bulk", user_token=token, org_id=org_id)
    r1 = create_report(title="One", user_token=token, org_id=org_id, data_sources=[])
    r2 = create_report(title="Two", user_token=token, org_id=org_id, data_sources=[])

    resp = test_client.post(
        "/api/reports/bulk/move",
        json={"report_ids": [r1["id"], r2["id"]], "project_id": project["id"]},
        headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["moved"] == 2

    in_project = list_reports(user_token=token, org_id=org_id,
                              filter="all", project_id=project["id"])
    assert {r1["id"], r2["id"]} == {r["id"] for r in in_project["reports"]}

    # Clearing via bulk move with project_id=None.
    resp = test_client.post(
        "/api/reports/bulk/move",
        json={"report_ids": [r1["id"]], "project_id": None},
        headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
    )
    assert resp.status_code == 200
    in_project = list_reports(user_token=token, org_id=org_id,
                              filter="all", project_id=project["id"])
    assert {r["id"] for r in in_project["reports"]} == {r2["id"]}


@pytest.mark.e2e
def test_cannot_move_someone_elses_report(
    test_client, create_project, create_report, move_report_to_project,
    upsert_project_member, create_user, login_user, whoami,
):
    token1, token2, org_id, member_id = _setup_two_users(create_user, login_user, whoami, test_client)

    # Owner's report; member has a project of their own.
    report = create_report(title="Not yours", user_token=token1, org_id=org_id, data_sources=[])
    member_project = create_project(name="Member Folder", user_token=token2, org_id=org_id)

    # Member cannot move the owner's report (owner_only on the update route).
    move_report_to_project(report["id"], member_project["id"],
                           user_token=token2, org_id=org_id, expect_status=403)

    # Bulk endpoint enforces the same ownership rule.
    resp = test_client.post(
        "/api/reports/bulk/move",
        json={"report_ids": [report["id"]], "project_id": member_project["id"]},
        headers={"Authorization": f"Bearer {token2}", "X-Organization-Id": org_id},
    )
    assert resp.status_code == 403


@pytest.mark.e2e
def test_cannot_move_report_into_invisible_project(
    test_client, create_project, create_report, move_report_to_project,
    create_user, login_user, whoami,
):
    token1, token2, org_id, _ = _setup_two_users(create_user, login_user, whoami, test_client)

    private_project = create_project(name="Private", user_token=token1, org_id=org_id)
    my_report = create_report(title="Mine", user_token=token2, org_id=org_id, data_sources=[])

    # Member can't target a project they can't see — and the error must be
    # a 404, indistinguishable from a nonexistent project.
    move_report_to_project(my_report["id"], private_project["id"],
                           user_token=token2, org_id=org_id, expect_status=404)
    move_report_to_project(my_report["id"], str(uuid.uuid4()),
                           user_token=token2, org_id=org_id, expect_status=404)


@pytest.mark.e2e
def test_listing_project_reports_requires_project_access(
    test_client, create_project, create_report, list_reports, move_report_to_project,
    upsert_project_member, create_user, login_user, whoami,
):
    token1, token2, org_id, member_id = _setup_two_users(create_user, login_user, whoami, test_client)

    project = create_project(name="Scoped", user_token=token1, org_id=org_id)
    report = create_report(title="Scoped Report", user_token=token1, org_id=org_id, data_sources=[])
    move_report_to_project(report["id"], project["id"], user_token=token1, org_id=org_id)

    # Without a grant: 404 on the filtered list.
    list_reports(user_token=token2, org_id=org_id, filter="all",
                 project_id=project["id"], expect_status=404)

    # With a view grant: the member sees the owner's report in the project.
    upsert_project_member(project["id"], member_id, permissions=["view"],
                          user_token=token1, org_id=org_id)
    in_project = list_reports(user_token=token2, org_id=org_id, filter="all",
                              project_id=project["id"])
    assert report["id"] in {r["id"] for r in in_project["reports"]}


@pytest.mark.e2e
def test_delete_project_archives_reports_and_stops_tasks(
    test_client, create_project, create_report, delete_project, list_reports,
    move_report_to_project, create_scheduled_prompt,
    create_user, login_user, whoami,
):
    """Deleting a project behaves like deleting its reports: they are archived
    (hidden from every list, data retained) and their scheduled prompts stop."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}

    project = create_project(name="Doomed", user_token=token, org_id=org_id)
    report = create_report(title="Hidden", user_token=token, org_id=org_id, data_sources=[])
    move_report_to_project(report["id"], project["id"], user_token=token, org_id=org_id)
    create_scheduled_prompt(report["id"], user_token=token, org_id=org_id)

    delete_project(project["id"], user_token=token, org_id=org_id)

    # Hidden from the owner's lists (root and unfiltered) — archived, not moved.
    root = list_reports(user_token=token, org_id=org_id, filter="my", project_id="none")
    assert report["id"] not in {r["id"] for r in root["reports"]}
    all_mine = list_reports(user_token=token, org_id=org_id, filter="my")
    assert report["id"] not in {r["id"] for r in all_mine["reports"]}

    # Data retained: the owner can still fetch it directly, in archived state.
    direct = test_client.get(f"/api/reports/{report['id']}", headers=headers)
    assert direct.status_code == 200
    assert direct.json()["status"] == "archived"

    # Scheduled prompts on contained reports are stopped (soft-deleted).
    sps = test_client.get(f"/api/reports/{report['id']}/scheduled-prompts", headers=headers)
    assert sps.status_code == 200
    active = [sp for sp in sps.json() if not sp.get("deleted_at")]
    assert active == []


@pytest.mark.e2e
def test_minimal_view_carries_project_id(
    create_project, create_report, list_reports, move_report_to_project,
    create_user, login_user, whoami,
):
    """The sidebar's lightweight list must expose project_id for grouping."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    project = create_project(name="Sidebar", user_token=token, org_id=org_id)
    report = create_report(title="Grouped", user_token=token, org_id=org_id, data_sources=[])
    move_report_to_project(report["id"], project["id"], user_token=token, org_id=org_id)

    minimal = list_reports(user_token=token, org_id=org_id, filter="my", view="minimal")
    row = next((r for r in minimal["reports"] if r["id"] == report["id"]), None)
    assert row is not None
    assert row["project_id"] == project["id"]


@pytest.mark.e2e
def test_project_report_count(
    create_project, create_report, get_project, move_report_to_project, delete_report,
    create_user, login_user, whoami,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    project = create_project(name="Counted", user_token=token, org_id=org_id)
    r1 = create_report(title="A", user_token=token, org_id=org_id, data_sources=[])
    r2 = create_report(title="B", user_token=token, org_id=org_id, data_sources=[])
    move_report_to_project(r1["id"], project["id"], user_token=token, org_id=org_id)
    move_report_to_project(r2["id"], project["id"], user_token=token, org_id=org_id)

    fetched = get_project(project["id"], user_token=token, org_id=org_id).json()
    assert fetched["report_count"] == 2

    # Archived reports don't count.
    delete_report(r2["id"], user_token=token, org_id=org_id)
    fetched = get_project(project["id"], user_token=token, org_id=org_id).json()
    assert fetched["report_count"] == 1


@pytest.mark.e2e
def test_projects_are_org_scoped(
    _allow_multiple_orgs,
    create_project, get_project, list_projects, create_organization,
    create_user, login_user, whoami,
):
    """A project id resolves only within its own organization — even for its
    owner acting under a different org context."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org1 = whoami(token)["organizations"][0]["id"]
    project = create_project(name="Org1 Project", user_token=token, org_id=org1)

    org2 = create_organization(name=f"Second Org {uuid.uuid4().hex[:6]}", user_token=token)
    get_project(project["id"], user_token=token, org_id=org2, expect_status=404)
    listed = list_projects(user_token=token, org_id=org2)
    assert project["id"] not in _project_ids(listed)
