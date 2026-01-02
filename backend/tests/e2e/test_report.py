import pytest
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
