import pytest
import uuid
from fastapi.testclient import TestClient
from main import app
from tests.utils.user_creds import main_user

@pytest.mark.e2e
def test_report_creation(
    create_report,
    create_user,
    login_user,
    whoami
):
    # Setup user and organization
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create a basic report matching frontend implementation
    report = create_report(
        title="Test Report",
        user_token=user_token,
        org_id=org_id,
        data_sources=[]
    )

    # Basic assertions
    assert report is not None
    assert report["title"] == "Test Report"
    assert "id" in report
    assert "status" in report
    assert "slug" in report
    assert "widgets" in report
    assert isinstance(report["widgets"], list)


def test_report_create_and_publish(
    create_report,
    create_user,
    login_user,
    whoami,
    publish_report
):
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    report = create_report(
        title="Test Report",
        user_token=user_token,
        org_id=org_id,
        widget=None,
        files=[],
        data_sources=[]
    )
    assert report is not None
    # Publish the report
    report = publish_report(report_id=report["id"], user_token=user_token, org_id=org_id)
    assert report["status"] == "published"

    # Unpublish the report
    report = publish_report(report_id=report["id"], user_token=user_token, org_id=org_id)
    assert report["status"] == "draft"


@pytest.mark.e2e
def test_get_report_includes_conversation_share_status(
    test_client,
    create_report,
    get_report,
    create_user,
    login_user,
    whoami,
):
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    report = create_report(
        title="Test Report - Share Status",
        user_token=user_token,
        org_id=org_id,
        widget=None,
        files=[],
        data_sources=[],
    )

    headers = {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id),
    }

    # Enable sharing (endpoint is a toggle; report starts disabled)
    resp = test_client.post(f"/api/reports/{report['id']}/conversation-share", headers=headers)
    assert resp.status_code == 200, resp.json()
    payload = resp.json()
    assert payload["enabled"] is True
    assert isinstance(payload.get("token"), str) and len(payload["token"]) > 0

    # GET /reports/{id} should reflect the real share status + token
    fetched = get_report(report["id"], user_token=user_token, org_id=org_id)
    assert fetched["conversation_share_enabled"] is True
    assert fetched["conversation_share_token"] == payload["token"]


# --- Fork Report Tests ---

def _setup_two_users(create_user, login_user, whoami, add_organization_member):
    """Helper: create two users in the same organization. Returns (user1_token, user2_token, org_id, user2_id)."""
    # User 1 (owner) — auto-gets an org on first registration
    email1 = f"fork_owner_{uuid.uuid4().hex[:6]}@test.com"
    user1 = create_user(email=email1, password="test123")
    token1 = login_user(email=email1, password="test123")
    me1 = whoami(token1)
    org_id = me1["organizations"][0]["id"]

    # User 2 (forker)
    email2 = f"fork_user_{uuid.uuid4().hex[:6]}@test.com"
    user2 = create_user(email=email2, password="test123")
    token2 = login_user(email=email2, password="test123")
    me2 = whoami(token2)
    user2_id = me2["id"]

    # Add user2 to the same org
    add_organization_member(org_id, user2_id, role_id="member", token=token1)

    return token1, token2, org_id, user2_id


@pytest.mark.e2e
def test_fork_published_report(
    test_client,
    create_report,
    create_user,
    login_user,
    whoami,
    publish_report,
    fork_report,
    add_organization_member,
):
    """Forking a published report creates a new draft report owned by the forking user."""
    token1, token2, org_id, _ = _setup_two_users(create_user, login_user, whoami, add_organization_member)

    # Owner creates and publishes a report
    report = create_report(title="Original Analysis", user_token=token1, org_id=org_id)
    publish_report(report_id=report["id"], user_token=token1, org_id=org_id)

    # User2 forks it
    resp = fork_report(report["id"], user_token=token2, org_id=org_id)
    fork = resp.json()

    assert fork["forked_from_id"] == report["id"]
    assert fork["title"] == "Fork of Original Analysis"
    assert "id" in fork
    assert "slug" in fork
    assert fork["id"] != report["id"]


@pytest.mark.e2e
def test_fork_with_custom_title(
    test_client,
    create_report,
    create_user,
    login_user,
    whoami,
    publish_report,
    fork_report,
    add_organization_member,
):
    """Forking with a custom title uses that title instead of the default."""
    token1, token2, org_id, _ = _setup_two_users(create_user, login_user, whoami, add_organization_member)

    report = create_report(title="Revenue Report", user_token=token1, org_id=org_id)
    publish_report(report_id=report["id"], user_token=token1, org_id=org_id)

    resp = fork_report(report["id"], user_token=token2, org_id=org_id, title="My Custom Fork")
    fork = resp.json()

    assert fork["title"] == "My Custom Fork"
    assert fork["forked_from_id"] == report["id"]


@pytest.mark.e2e
def test_fork_own_report_forbidden(
    test_client,
    create_report,
    create_user,
    login_user,
    whoami,
    publish_report,
    fork_report,
):
    """Owner cannot fork their own report."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    report = create_report(title="My Report", user_token=token, org_id=org_id)
    publish_report(report_id=report["id"], user_token=token, org_id=org_id)

    resp = fork_report(report["id"], user_token=token, org_id=org_id, expect_status=None)
    assert resp.status_code == 403
    assert "is_owner" in resp.json().get("detail", "")


@pytest.mark.e2e
def test_fork_unpublished_report_forbidden(
    test_client,
    create_report,
    create_user,
    login_user,
    whoami,
    fork_report,
    add_organization_member,
):
    """Cannot fork a draft (unpublished) report."""
    token1, token2, org_id, _ = _setup_two_users(create_user, login_user, whoami, add_organization_member)

    report = create_report(title="Draft Report", user_token=token1, org_id=org_id)
    # NOT published

    resp = fork_report(report["id"], user_token=token2, org_id=org_id, expect_status=None)
    assert resp.status_code == 403


@pytest.mark.e2e
def test_forked_report_has_lineage_fields(
    test_client,
    create_report,
    get_report,
    create_user,
    login_user,
    whoami,
    publish_report,
    fork_report,
    add_organization_member,
):
    """GET /reports/{id} on the forked report includes fork lineage fields."""
    token1, token2, org_id, _ = _setup_two_users(create_user, login_user, whoami, add_organization_member)

    report = create_report(title="Source Report", user_token=token1, org_id=org_id)
    publish_report(report_id=report["id"], user_token=token1, org_id=org_id)

    resp = fork_report(report["id"], user_token=token2, org_id=org_id)
    fork_data = resp.json()

    # Fetch the forked report as its owner (user2)
    forked = get_report(fork_data["id"], user_token=token2, org_id=org_id)

    assert forked["forked_from_id"] == report["id"]
    assert forked["forked_from_title"] == "Source Report"
    assert forked["status"] == "draft"


@pytest.mark.e2e
def test_public_report_includes_fork_eligibility(
    test_client,
    create_report,
    create_user,
    login_user,
    whoami,
    publish_report,
):
    """GET /r/{id} returns fork_eligibility for anonymous users."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    report = create_report(title="Public Report", user_token=token, org_id=org_id)
    publish_report(report_id=report["id"], user_token=token, org_id=org_id)

    # Fetch public report without auth
    resp = test_client.get(f"/api/r/{report['id']}")
    assert resp.status_code == 200
    data = resp.json()

    assert "fork_eligibility" in data
    assert data["fork_eligibility"]["can_fork"] is False
    assert data["fork_eligibility"]["reason"] == "not_logged_in"


@pytest.mark.e2e
def test_public_report_fork_eligibility_for_logged_in_user(
    test_client,
    create_report,
    create_user,
    login_user,
    whoami,
    publish_report,
    add_organization_member,
):
    """GET /r/{id} with auth returns can_fork=true for eligible user."""
    token1, token2, org_id, _ = _setup_two_users(create_user, login_user, whoami, add_organization_member)

    report = create_report(title="Public Eligible", user_token=token1, org_id=org_id)
    publish_report(report_id=report["id"], user_token=token1, org_id=org_id)

    # Fetch public report WITH user2's auth
    resp = test_client.get(
        f"/api/r/{report['id']}",
        headers={"Authorization": f"Bearer {token2}"}
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["fork_eligibility"]["can_fork"] is True
    assert data["fork_eligibility"]["reason"] is None


@pytest.mark.e2e
def test_fork_creates_summary_completion(
    test_client,
    create_report,
    create_user,
    login_user,
    whoami,
    publish_report,
    fork_report,
    get_completions,
    add_organization_member,
):
    """Forking creates a fork summary completion as the first message."""
    token1, token2, org_id, _ = _setup_two_users(create_user, login_user, whoami, add_organization_member)

    report = create_report(title="Analysis Report", user_token=token1, org_id=org_id)
    publish_report(report_id=report["id"], user_token=token1, org_id=org_id)

    resp = fork_report(report["id"], user_token=token2, org_id=org_id)
    fork_data = resp.json()

    # Fetch completions for the forked report
    completions = get_completions(report_id=fork_data["id"], user_token=token2, org_id=org_id)

    # Should have at least 1 completion (the fork summary)
    assert isinstance(completions, list)
    assert len(completions) >= 1

    # Find the fork summary
    summary = None
    for c in completions:
        if c.get("is_fork_summary"):
            summary = c
            break

    assert summary is not None, "Fork summary completion not found"
    assert summary["role"] == "system"
    assert summary["source_report_id"] == report["id"]
    assert isinstance(summary.get("fork_asset_refs"), list)
