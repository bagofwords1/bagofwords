import pytest

@pytest.fixture
def create_report(test_client):
    def _create_report(title="Test Report", user_token=None, org_id=None, widget=None, files=None, data_sources=None):
        if user_token is None:
            pytest.fail("User token is required for create_report")
        if org_id is None:
            pytest.fail("Organization ID is required for create_report")
        
        payload = {
            "title": title,
            "widget": widget or None,
            "files": files or [],
            "data_sources": data_sources or []
        }
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.post(
            "/api/reports",
            json=payload,
            headers=headers
        )
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _create_report

@pytest.fixture
def get_reports(test_client):
    def _get_reports(user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for get_reports")
        if org_id is None:
            pytest.fail("Organization ID is required for get_reports")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.get(
            "/api/reports",
            headers=headers
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _get_reports

@pytest.fixture
def get_report(test_client):
    def _get_report(report_id, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for get_report")
        if org_id is None:
            pytest.fail("Organization ID is required for get_report")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.get(
            f"/api/reports/{report_id}",
            headers=headers
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _get_report

@pytest.fixture
def update_report(test_client):
    def _update_report(report_id, title, status=None, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for update_report")
        if org_id is None:
            pytest.fail("Organization ID is required for update_report")
        
        payload = {
            "title": title,
            "status": status
        }
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.put(
            f"/api/reports/{report_id}",
            json=payload,
            headers=headers
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _update_report

@pytest.fixture
def delete_report(test_client):
    def _delete_report(report_id, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for delete_report")
        if org_id is None:
            pytest.fail("Organization ID is required for delete_report")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.delete(
            f"/api/reports/{report_id}",
            headers=headers
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _delete_report

@pytest.fixture
def publish_report(test_client):
    def _publish_report(report_id, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for publish_report")
        if org_id is None:
            pytest.fail("Organization ID is required for publish_report")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.post(
            f"/api/reports/{report_id}/publish",
            headers=headers
        )
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _publish_report

@pytest.fixture
def rerun_report(test_client):
    def _rerun_report(report_id, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for rerun_report")
        if org_id is None:
            pytest.fail("Organization ID is required for rerun_report")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.post(
            f"/api/reports/{report_id}/rerun",
            headers=headers
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _rerun_report

@pytest.fixture
def schedule_report(test_client):
    def _schedule_report(report_id, cron_expression, user_token=None, org_id=None, notification_subscribers=None):
        if user_token is None:
            pytest.fail("User token is required for schedule_report")
        if org_id is None:
            pytest.fail("Organization ID is required for schedule_report")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }

        body = {"cron_expression": cron_expression}
        if notification_subscribers is not None:
            body["notification_subscribers"] = notification_subscribers

        response = test_client.post(
            f"/api/reports/{report_id}/schedule",
            json=body,
            headers=headers
        )

        assert response.status_code == 200, response.json()
        return response.json()

    return _schedule_report

@pytest.fixture
def get_public_report(test_client):
    def _get_public_report(report_id):
        response = test_client.get(
            f"/api/r/{report_id}"
        )

        assert response.status_code == 200, response.json()
        return response.json()

    return _get_public_report


@pytest.fixture
def set_visibility(test_client):
    def _set_visibility(report_id, share_type, visibility, user_token=None, org_id=None, shared_user_ids=None, expect_status=200):
        if user_token is None:
            pytest.fail("User token is required for set_visibility")
        if org_id is None:
            pytest.fail("Organization ID is required for set_visibility")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }

        body = {"visibility": visibility}
        if shared_user_ids is not None:
            body["shared_user_ids"] = shared_user_ids

        response = test_client.put(
            f"/api/reports/{report_id}/visibility/{share_type}",
            json=body,
            headers=headers
        )
        if expect_status:
            assert response.status_code == expect_status, response.json()
        return response

    return _set_visibility


@pytest.fixture
def get_shares(test_client):
    def _get_shares(report_id, share_type, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for get_shares")
        if org_id is None:
            pytest.fail("Organization ID is required for get_shares")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }

        response = test_client.get(
            f"/api/reports/{report_id}/shares/{share_type}",
            headers=headers
        )
        assert response.status_code == 200, response.json()
        return response.json()

    return _get_shares


@pytest.fixture
def fork_report(test_client):
    def _fork_report(report_id, user_token=None, org_id=None, title=None, expect_status=200):
        if user_token is None:
            pytest.fail("User token is required for fork_report")
        if org_id is None:
            pytest.fail("Organization ID is required for fork_report")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }

        payload = {}
        if title is not None:
            payload["title"] = title

        response = test_client.post(
            f"/api/reports/{report_id}/fork",
            json=payload,
            headers=headers
        )
        if expect_status:
            assert response.status_code == expect_status, response.json()
        return response

    return _fork_report