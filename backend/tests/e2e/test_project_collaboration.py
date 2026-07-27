"""
E2E tests for project collaboration (single collaborator role) and defaults.

Covers:
- Collaborators (view grant) auto-see project reports READ-ONLY:
  GET /reports/{id} + completions list work; posting a completion is 403.
- Collaborators can fork a project report; the fork lands in their root list.
- Project defaults: PUT /projects/{id}/files + instructions; new reports in
  the project inherit default files; instructions round-trip on the project.
- Default agents: applied only when the creator picks none (explicit
  selection overrides, never unions).
"""
import pytest
import uuid
from pathlib import Path

DATA_SOURCE_TEST_DB_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite"
).resolve()


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
def test_project_files_inherited_live_into_context(
    test_client, create_project, upload_file, move_report_to_project,
    create_report, create_user, login_user, whoami,
):
    """Project files are inherited LIVE: a file added to the project AFTER a
    report exists appears in that report's files context and file-tool space;
    moving the report out removes it. (No copy at creation.)"""
    import asyncio
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.report import Report
    from app.ai.context.builders.files_context_builder import FilesContextBuilder
    from app.ai.tools.implementations._file_tool_common import resolve_session_file
    from app.models.organization import Organization
    from app.services.project_service import project_service

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    project = create_project(name="Live Files", user_token=token, org_id=org_id)
    # Report exists BEFORE the file is added to the project.
    report = create_report(title="Pre-existing", user_token=token, org_id=org_id, data_sources=[])
    move_report_to_project(report["id"], project["id"], user_token=token, org_id=org_id)

    uploaded = upload_file(b"month,revenue\nJan,100\n", "live.csv", "text/csv",
                           user_token=token, org_id=org_id)
    resp = test_client.put(
        f"/api/projects/{project['id']}/files",
        json={"file_ids": [uploaded["id"]]},
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()

    async def _context_filenames_and_toolspace():
        async with async_session_maker() as db:
            r = (await db.execute(
                select(Report).where(Report.id == report["id"])
            )).scalar_one()
            org = (await db.execute(
                select(Organization).where(Organization.id == org_id)
            )).scalar_one()
            ctx = await FilesContextBuilder(db, org, r).build()
            pfiles = await project_service.get_project_files_for_report(db, r)
            resolved = resolve_session_file(
                {"report": r, "organization": org, "project_files": pfiles},
                uploaded["id"],
            )
            return (
                {(f.filename, f.origin) for f in ctx.files},
                resolved is not None,
            )

    names, tool_resolves = asyncio.run(_context_filenames_and_toolspace())
    assert ("live.csv", "project") in names
    assert tool_resolves is True

    # The file was NOT copied onto the report's own association.
    files_resp = test_client.get(f"/api/reports/{report['id']}/files", headers=_headers(token, org_id))
    assert uploaded["id"] not in {f["id"] for f in files_resp.json()}

    # Moving the report out removes the inherited file from its context.
    move_report_to_project(report["id"], None, user_token=token, org_id=org_id)
    names_after, _ = asyncio.run(_context_filenames_and_toolspace())
    assert ("live.csv", "project") not in names_after


@pytest.mark.e2e
def test_project_automations_endpoint(
    test_client, create_project, create_report, move_report_to_project,
    create_scheduled_prompt, schedule_report, upsert_project_member,
    create_user, login_user, whoami,
):
    """GET /projects/{id}/automations aggregates scheduled tasks and dashboard
    refreshes of the project's reports, and is visible to collaborators."""
    token1, token2, org_id, member_id = _setup_owner_and_collaborator(
        create_user, login_user, whoami, test_client)

    project = create_project(name="Automated", user_token=token1, org_id=org_id)
    r1 = create_report(title="Tasked", user_token=token1, org_id=org_id, data_sources=[])
    r2 = create_report(title="Refreshed", user_token=token1, org_id=org_id, data_sources=[])
    move_report_to_project(r1["id"], project["id"], user_token=token1, org_id=org_id)
    move_report_to_project(r2["id"], project["id"], user_token=token1, org_id=org_id)
    create_scheduled_prompt(r1["id"], prompt={"content": "Weekly revenue rollup"},
                            user_token=token1, org_id=org_id)
    schedule_report(r2["id"], "0 9 * * 1", user_token=token1, org_id=org_id)

    resp = test_client.get(f"/api/projects/{project['id']}/automations",
                           headers=_headers(token1, org_id))
    assert resp.status_code == 200, resp.json()
    items = resp.json()
    kinds = {(i["kind"], i["report_id"]) for i in items}
    assert ("task", r1["id"]) in kinds
    assert ("refresh", r2["id"]) in kinds
    task = next(i for i in items if i["kind"] == "task")
    assert "Weekly revenue rollup" in task["label"]
    assert task["is_active"] is True

    # Invisible to non-members (404, no existence leak); visible after a grant.
    resp = test_client.get(f"/api/projects/{project['id']}/automations",
                           headers=_headers(token2, org_id))
    assert resp.status_code == 404
    upsert_project_member(project["id"], member_id, permissions=["view"],
                          user_token=token1, org_id=org_id)
    resp = test_client.get(f"/api/projects/{project['id']}/automations",
                           headers=_headers(token2, org_id))
    assert resp.status_code == 200
    assert len(resp.json()) == 2


@pytest.mark.e2e
def test_project_default_agents_override_not_union(
    test_client, create_project, create_data_source, create_report,
    create_user, login_user, whoami,
):
    """Default agents land on a new project report only when the creator
    picked none; an explicit selection overrides them (no union)."""
    if not DATA_SOURCE_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {DATA_SOURCE_TEST_DB_PATH}")

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    ds_default = create_data_source(
        name="Project Default DS", type="sqlite",
        config={"database": str(DATA_SOURCE_TEST_DB_PATH)}, credentials={},
        user_token=token, org_id=org_id)
    ds_explicit = create_data_source(
        name="Explicitly Picked DS", type="sqlite",
        config={"database": str(DATA_SOURCE_TEST_DB_PATH)}, credentials={},
        user_token=token, org_id=org_id)

    project = create_project(name="Defaulted", user_token=token, org_id=org_id)
    resp = test_client.put(
        f"/api/projects/{project['id']}/data_sources",
        json={"data_source_ids": [ds_default["id"]]},
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()

    # No explicit selection → the project default applies.
    implicit = create_report(title="Implicit", user_token=token, org_id=org_id,
                             data_sources=[], project_id=project["id"])
    got = test_client.get(f"/api/reports/{implicit['id']}", headers=_headers(token, org_id)).json()
    assert {d["id"] for d in got["data_sources"]} == {ds_default["id"]}

    # Explicit selection → exactly that selection, defaults NOT union-ed in.
    explicit = create_report(title="Explicit", user_token=token, org_id=org_id,
                             data_sources=[ds_explicit["id"]], project_id=project["id"])
    got = test_client.get(f"/api/reports/{explicit['id']}", headers=_headers(token, org_id)).json()
    assert {d["id"] for d in got["data_sources"]} == {ds_explicit["id"]}


@pytest.mark.e2e
def test_minimal_report_list_carries_project_mini(
    test_client, create_project, create_report, move_report_to_project,
    create_user, login_user, whoami,
):
    """The sidebar list (view=minimal, no project filter) includes reports
    inside projects and hydrates the project mini (name/color) for the
    tinted icon + tooltip."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    project = create_project(name="Tinted", user_token=token, org_id=org_id, color="#f97316")
    inside = create_report(title="In Folder", user_token=token, org_id=org_id, data_sources=[])
    outside = create_report(title="At Root", user_token=token, org_id=org_id, data_sources=[])
    move_report_to_project(inside["id"], project["id"], user_token=token, org_id=org_id)

    resp = test_client.get("/api/reports", params={"filter": "my", "view": "minimal", "limit": 50},
                           headers=_headers(token, org_id))
    assert resp.status_code == 200, resp.json()
    rows = {r["id"]: r for r in resp.json()["reports"]}
    assert inside["id"] in rows and outside["id"] in rows
    assert rows[inside["id"]]["project"]["name"] == "Tinted"
    assert rows[inside["id"]]["project"]["color"] == "#f97316"
    assert rows[outside["id"]]["project"] is None


@pytest.mark.e2e
def test_project_schema_counts_for_delete_confirmation(
    test_client, create_project, create_report, move_report_to_project,
    create_scheduled_prompt, schedule_report, get_project,
    create_user, login_user, whoami,
):
    """dashboard_count and automation_count on ProjectSchema feed the
    delete-confirmation copy (archive X reports…, stop Z automations…)."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    project = create_project(name="Counted", user_token=token, org_id=org_id)
    r1 = create_report(title="Tasked", user_token=token, org_id=org_id, data_sources=[])
    r2 = create_report(title="Refreshed", user_token=token, org_id=org_id, data_sources=[])
    move_report_to_project(r1["id"], project["id"], user_token=token, org_id=org_id)
    move_report_to_project(r2["id"], project["id"], user_token=token, org_id=org_id)
    create_scheduled_prompt(r1["id"], prompt={"content": "Nightly rollup"},
                            user_token=token, org_id=org_id)
    schedule_report(r2["id"], "0 6 * * *", user_token=token, org_id=org_id)

    fetched = get_project(project["id"], user_token=token, org_id=org_id).json()
    assert fetched["report_count"] == 2
    assert fetched["automation_count"] == 2  # one task + one refresh
    assert fetched["dashboard_count"] == 0   # no artifacts in this env


@pytest.mark.e2e
def test_member_invite_and_agent_add_respect_agent_access(
    test_client, create_project, create_data_source, upsert_project_member,
    create_user, login_user, whoami,
):
    """Invariant: every project member can resolve every default agent.
    - Inviting a user who can't see a default agent → 400.
    - Adding an agent an existing member can't see → 400 (stale-UI backstop).
    - The selectable-agents endpoint never offers such an agent.
    - Granting the member DS access lifts all three restrictions."""
    if not DATA_SOURCE_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {DATA_SOURCE_TEST_DB_PATH}")

    token1, token2, org_id, member_id = _setup_owner_and_collaborator(
        create_user, login_user, whoami, test_client)

    # A restricted agent (is_public defaults to False): the owner/admin sees
    # it, the plain member does not.
    ds = create_data_source(
        name="Restricted DWH", type="sqlite",
        config={"database": str(DATA_SOURCE_TEST_DB_PATH)}, credentials={},
        user_token=token1, org_id=org_id)

    project = create_project(name="Gated", user_token=token1, org_id=org_id)
    resp = test_client.put(
        f"/api/projects/{project['id']}/data_sources",
        json={"data_source_ids": [ds["id"]]},
        headers=_headers(token1, org_id),
    )
    assert resp.status_code == 200, resp.json()

    # 1) Invite blocked: the member can't resolve the default agent.
    resp = upsert_project_member(project["id"], member_id, permissions=["view"],
                                 user_token=token1, org_id=org_id, expect_status=400)
    assert "Restricted DWH" in resp.json()["detail"]

    # Clear the default → the invite goes through.
    resp = test_client.put(
        f"/api/projects/{project['id']}/data_sources",
        json={"data_source_ids": []},
        headers=_headers(token1, org_id),
    )
    assert resp.status_code == 200
    upsert_project_member(project["id"], member_id, permissions=["view"],
                          user_token=token1, org_id=org_id)

    # 2) Stale-UI backstop: re-adding the restricted agent now 400s because
    # an existing member can't see it.
    resp = test_client.put(
        f"/api/projects/{project['id']}/data_sources",
        json={"data_source_ids": [ds["id"]]},
        headers=_headers(token1, org_id),
    )
    assert resp.status_code == 400
    assert "Restricted DWH" in resp.json()["detail"]

    # 3) The picker source never offers it.
    resp = test_client.get(
        f"/api/projects/{project['id']}/data_sources/selectable",
        headers=_headers(token1, org_id),
    )
    assert resp.status_code == 200, resp.json()
    assert ds["id"] not in {a["id"] for a in resp.json()}

    # 4) Granting the member access to the agent lifts the restriction.
    resp = test_client.post(
        f"/api/data_sources/{ds['id']}/members",
        json={"principal_type": "user", "principal_id": member_id},
        headers=_headers(token1, org_id),
    )
    assert resp.status_code == 200, resp.json()
    resp = test_client.get(
        f"/api/projects/{project['id']}/data_sources/selectable",
        headers=_headers(token1, org_id),
    )
    assert ds["id"] in {a["id"] for a in resp.json()}
    resp = test_client.put(
        f"/api/projects/{project['id']}/data_sources",
        json={"data_source_ids": [ds["id"]]},
        headers=_headers(token1, org_id),
    )
    assert resp.status_code == 200, resp.json()


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
