"""
E2E tests for project collaboration (single collaborator role) and defaults.

Covers:
- Collaborators (view grant) auto-see project reports READ-ONLY:
  GET /reports/{id} + completions list work; posting a completion is 403.
- Collaborators can fork a project report; the fork lands in their root list.
- Project defaults: PUT /projects/{id}/files + instructions; new reports in
  the project inherit default files; instructions round-trip on the project.
"""
import pytest
import uuid


def _setup_owner_and_collaborator(create_user, login_user, whoami, test_client):
    email1 = f"collab_owner_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email1, password="test123")
    token1 = login_user(email=email1, password="test123")
    org_id = whoami(token1)["organizations"][0]["id"]

    email2 = f"collab_member_{uuid.uuid4().hex[:6]}@test.com"
    test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": email2, "role": "member"},
        headers={"Authorization": f"Bearer {token1}", "X-Organization-Id": org_id},
    )
    create_user(email=email2, password="test123")
    token2 = login_user(email=email2, password="test123")
    member_id = whoami(token2)["id"]
    return token1, token2, org_id, member_id


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}


@pytest.mark.e2e
def test_collaborator_reads_report_but_cannot_post(
    test_client, create_project, create_report, move_report_to_project,
    upsert_project_member, create_user, login_user, whoami,
):
    token1, token2, org_id, member_id = _setup_owner_and_collaborator(
        create_user, login_user, whoami, test_client)

    project = create_project(name="Team Space", user_token=token1, org_id=org_id)
    report = create_report(title="Team Analysis", user_token=token1, org_id=org_id, data_sources=[])
    move_report_to_project(report["id"], project["id"], user_token=token1, org_id=org_id)

    # Before the grant: the report is invisible to the member.
    resp = test_client.get(f"/api/reports/{report['id']}", headers=_headers(token2, org_id))
    assert resp.status_code in (403, 404)

    upsert_project_member(project["id"], member_id, permissions=["view"],
                          user_token=token1, org_id=org_id)

    # Read access: full report + conversation + summary endpoints.
    resp = test_client.get(f"/api/reports/{report['id']}", headers=_headers(token2, org_id))
    assert resp.status_code == 200, resp.json()
    assert resp.json()["title"] == "Team Analysis"
    resp = test_client.get(f"/api/reports/{report['id']}/completions", headers=_headers(token2, org_id))
    assert resp.status_code == 200

    # Write access: denied — the composer is owner-only on project reports.
    resp = test_client.post(
        f"/api/reports/{report['id']}/completions",
        json={"prompt": {"content": "hijack attempt"}},
        headers=_headers(token2, org_id),
    )
    assert resp.status_code == 403

    # The owner still chats normally on their own project report (guard must
    # not touch owners) — anything but 403 ownership rejection is fine here
    # (this env has no LLM configured, so a 400 about models is acceptable).
    resp = test_client.post(
        f"/api/reports/{report['id']}/completions",
        json={"prompt": {"content": "owner turn"}},
        headers=_headers(token1, org_id),
    )
    assert resp.status_code != 403


@pytest.mark.e2e
def test_collaborator_can_fork_project_report_to_own_root(
    test_client, create_project, create_report, move_report_to_project,
    upsert_project_member, list_reports, create_user, login_user, whoami,
):
    token1, token2, org_id, member_id = _setup_owner_and_collaborator(
        create_user, login_user, whoami, test_client)

    project = create_project(name="Forkable", user_token=token1, org_id=org_id)
    report = create_report(title="Original", user_token=token1, org_id=org_id, data_sources=[])
    move_report_to_project(report["id"], project["id"], user_token=token1, org_id=org_id)

    # Without a grant, forking is denied (draft, unshared, invisible project).
    resp = test_client.post(f"/api/reports/{report['id']}/fork", json={},
                            headers=_headers(token2, org_id))
    assert resp.status_code == 403

    upsert_project_member(project["id"], member_id, permissions=["view"],
                          user_token=token1, org_id=org_id)

    resp = test_client.post(f"/api/reports/{report['id']}/fork", json={},
                            headers=_headers(token2, org_id))
    assert resp.status_code == 200, resp.json()
    fork = resp.json()
    assert fork["forked_from_id"] == report["id"]

    # The fork is the member's own report, in their personal ROOT list.
    root = list_reports(user_token=token2, org_id=org_id, filter="my", project_id="none")
    assert fork["id"] in {r["id"] for r in root["reports"]}


@pytest.mark.e2e
def test_project_instructions_roundtrip(
    test_client, create_project, get_project, update_project,
    create_user, login_user, whoami,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    project = create_project(name="Guided", user_token=token, org_id=org_id)
    updated = update_project(project["id"], user_token=token, org_id=org_id,
                             instructions="Always report revenue in EUR.").json()
    assert updated["instructions"] == "Always report revenue in EUR."

    fetched = get_project(project["id"], user_token=token, org_id=org_id).json()
    assert fetched["instructions"] == "Always report revenue in EUR."

    # Clearing with empty string.
    cleared = update_project(project["id"], user_token=token, org_id=org_id,
                             instructions="").json()
    assert cleared["instructions"] is None


@pytest.mark.e2e
def test_default_files_copied_onto_new_project_reports(
    test_client, create_project, get_project, upload_file,
    create_user, login_user, whoami,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    project = create_project(name="With Defaults", user_token=token, org_id=org_id)
    uploaded = upload_file(b"month,revenue\nJan,100\n", "defaults.csv", "text/csv",
                           user_token=token, org_id=org_id)
    file_id = uploaded["id"]

    resp = test_client.put(
        f"/api/projects/{project['id']}/files",
        json={"file_ids": [file_id]},
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    assert [f["id"] for f in resp.json()["files"]] == [file_id]

    # A report born in the project inherits the default file.
    resp = test_client.post(
        "/api/reports",
        json={"title": "inherits", "files": [], "data_sources": [],
              "project_id": project["id"]},
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    report_id = resp.json()["id"]
    files_resp = test_client.get(f"/api/reports/{report_id}/files", headers=_headers(token, org_id))
    assert files_resp.status_code == 200
    assert file_id in {f["id"] for f in files_resp.json()}

    # A report created OUTSIDE the project does not inherit it.
    resp = test_client.post(
        "/api/reports",
        json={"title": "root report", "files": [], "data_sources": []},
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200
    files_resp = test_client.get(f"/api/reports/{resp.json()['id']}/files", headers=_headers(token, org_id))
    assert file_id not in {f["id"] for f in files_resp.json()}


@pytest.mark.e2e
def test_defaults_require_manage_access(
    test_client, create_project, upsert_project_member,
    create_user, login_user, whoami,
):
    token1, token2, org_id, member_id = _setup_owner_and_collaborator(
        create_user, login_user, whoami, test_client)

    project = create_project(name="Locked Defaults", user_token=token1, org_id=org_id)
    upsert_project_member(project["id"], member_id, permissions=["view"],
                          user_token=token1, org_id=org_id)

    # A view-only collaborator cannot change defaults or instructions.
    resp = test_client.put(
        f"/api/projects/{project['id']}/files",
        json={"file_ids": []},
        headers=_headers(token2, org_id),
    )
    assert resp.status_code == 403
    resp = test_client.put(
        f"/api/projects/{project['id']}",
        json={"instructions": "sneaky"},
        headers=_headers(token2, org_id),
    )
    assert resp.status_code == 403
