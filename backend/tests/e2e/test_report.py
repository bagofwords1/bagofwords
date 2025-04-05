import pytest
from fastapi.testclient import TestClient
from main import app
from tests.utils.user_creds import main_user

@pytest.mark.e2e
def test_report_creation(
    create_report,
    create_user,
    login_user,
    create_organization
):
    # Setup user and organization
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = create_organization(user_token=user_token)

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
    create_organization,
    publish_report
):
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = create_organization(user_token=user_token)

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
