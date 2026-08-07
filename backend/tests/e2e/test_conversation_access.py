"""E2E tests for conversation (transcript) access control and share links.

A report's conversation is owner-only. Sharing the *dashboard* grants the
artifact surface (/r/{id}) and nothing else — in particular it must not open
the authoring workspace's transcript, which is what the share notification
used to link to.

Covers:
- GET /api/reports/{id}/completions: owner yes, full admin yes (read-only
  bypass), plain org member no, dashboard-share recipient no.
- POST /api/reports/{id}/completions: owner-only, admins included.
- Share notification links: /r/{id} for a dashboard share, /c/{token} for a
  conversation share — never /reports/{id}.
"""
import pytest
import uuid


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _setup_admin_and_member(create_user, login_user, whoami, test_client):
    """Org creator (full admin) + an invited plain member."""
    email1 = f"conv_admin_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email1, password="test123")
    admin_token = login_user(email=email1, password="test123")
    org_id = whoami(admin_token)["organizations"][0]["id"]

    email2 = f"conv_member_{uuid.uuid4().hex[:6]}@test.com"
    test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": email2, "role": "member"},
        headers=_headers(admin_token, org_id),
    )
    create_user(email=email2, password="test123")
    member_token = login_user(email=email2, password="test123")
    member_id = whoami(member_token)["id"]
    return admin_token, member_token, org_id, member_id


@pytest.mark.e2e
def test_plain_member_cannot_read_another_users_conversation(
    test_client, create_report, create_user, login_user, whoami,
):
    """A member with view_reports may not read a conversation they don't own."""
    admin_token, member_token, org_id, _ = _setup_admin_and_member(
        create_user, login_user, whoami, test_client)

    report = create_report(title="Admin's Private Analysis", user_token=admin_token,
                           org_id=org_id, data_sources=[])

    resp = test_client.get(f"/api/reports/{report['id']}/completions",
                           headers=_headers(member_token, org_id))
    assert resp.status_code == 403, resp.json()

    # Same gate on the legacy list and the re-attachable SSE watch stream.
    resp = test_client.get(f"/api/reports/{report['id']}/completions.legacy",
                           headers=_headers(member_token, org_id))
    assert resp.status_code == 403

    resp = test_client.get(
        f"/api/reports/{report['id']}/completions/{uuid.uuid4().hex}/stream",
        headers=_headers(member_token, org_id))
    assert resp.status_code == 403

    # The owner still reads their own conversation.
    resp = test_client.get(f"/api/reports/{report['id']}/completions",
                           headers=_headers(admin_token, org_id))
    assert resp.status_code == 200, resp.json()


@pytest.mark.e2e
def test_full_admin_can_read_any_conversation(
    test_client, create_report, create_user, login_user, whoami,
):
    """Full admins keep the read-only ownership bypass (view_* permissions)."""
    admin_token, member_token, org_id, _ = _setup_admin_and_member(
        create_user, login_user, whoami, test_client)

    report = create_report(title="Member's Analysis", user_token=member_token,
                           org_id=org_id, data_sources=[])

    resp = test_client.get(f"/api/reports/{report['id']}/completions",
                           headers=_headers(admin_token, org_id))
    assert resp.status_code == 200, resp.json()


@pytest.mark.e2e
def test_plain_member_cannot_post_into_another_users_conversation(
    test_client, create_report, create_user, login_user, whoami,
):
    """Writing a turn is owner-only — admins included (create_reports is not a
    view_* permission, so the admin bypass does not apply)."""
    admin_token, member_token, org_id, _ = _setup_admin_and_member(
        create_user, login_user, whoami, test_client)

    report = create_report(title="Admin's Private Analysis", user_token=admin_token,
                           org_id=org_id, data_sources=[])

    resp = test_client.post(
        f"/api/reports/{report['id']}/completions",
        json={"prompt": {"content": "inject a turn"}},
        headers=_headers(member_token, org_id),
    )
    assert resp.status_code == 403, resp.json()

    resp = test_client.post(
        f"/api/reports/{report['id']}/completions/estimate",
        json={"prompt": {"content": "how big is this context"}},
        headers=_headers(member_token, org_id),
    )
    assert resp.status_code == 403

    resp = test_client.post(f"/api/reports/{report['id']}/context/compact",
                            headers=_headers(member_token, org_id))
    assert resp.status_code == 403

    # The owner's own composer is untouched (no LLM configured in this env, so
    # anything but an ownership rejection is fine).
    resp = test_client.post(
        f"/api/reports/{report['id']}/completions",
        json={"prompt": {"content": "owner turn"}},
        headers=_headers(admin_token, org_id),
    )
    assert resp.status_code != 403


@pytest.mark.e2e
def test_dashboard_share_does_not_grant_the_conversation(
    test_client, create_report, set_visibility, create_user, login_user, whoami,
):
    """A shared dashboard grants /r/{id} only: the recipient reaches the report
    record but never its transcript."""
    admin_token, member_token, org_id, member_id = _setup_admin_and_member(
        create_user, login_user, whoami, test_client)

    report = create_report(title="Service Events by Depot", user_token=admin_token,
                           org_id=org_id, data_sources=[])
    set_visibility(report["id"], "artifact", "shared", user_token=admin_token,
                   org_id=org_id, shared_user_ids=[member_id])

    # The share grants the dashboard surface.
    resp = test_client.get(f"/api/r/{report['id']}", headers=_headers(member_token, org_id))
    assert resp.status_code == 200, resp.json()

    # ...and not the conversation.
    resp = test_client.get(f"/api/reports/{report['id']}/completions",
                           headers=_headers(member_token, org_id))
    assert resp.status_code == 403, resp.json()


@pytest.mark.e2e
def test_share_notification_links_to_the_shared_surface(
    test_client, create_report, set_visibility, create_user, login_user, whoami,
):
    """The in-app share notification points at the surface that was shared:
    /r/{id} for a dashboard, /c/{token} for a conversation."""
    admin_token, member_token, org_id, member_id = _setup_admin_and_member(
        create_user, login_user, whoami, test_client)

    report = create_report(title="Service Events by Depot", user_token=admin_token,
                           org_id=org_id, data_sources=[])
    set_visibility(report["id"], "artifact", "shared", user_token=admin_token,
                   org_id=org_id, shared_user_ids=[member_id])

    resp = test_client.get("/api/notifications?source=share",
                           headers=_headers(member_token, org_id))
    assert resp.status_code == 200, resp.json()
    items = resp.json()["items"]
    artifact_notes = [n for n in items if n["type"] == "share_artifact"]
    assert len(artifact_notes) == 1, items
    assert artifact_notes[0]["link"] == f"/r/{report['id']}"

    # Conversation share: the read-only transcript link, keyed by the token
    # set_visibility mints when conversation visibility leaves 'none'.
    vis = set_visibility(report["id"], "conversation", "shared", user_token=admin_token,
                         org_id=org_id, shared_user_ids=[member_id]).json()
    token = vis["conversation_share_token"]
    assert token

    resp = test_client.get("/api/notifications?source=share",
                           headers=_headers(member_token, org_id))
    conv_notes = [n for n in resp.json()["items"] if n["type"] == "share_conversation"]
    assert len(conv_notes) == 1, resp.json()["items"]
    assert conv_notes[0]["link"] == f"/c/{token}"
