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
    def _schedule_report(report_id, cron_expression, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for schedule_report")
        if org_id is None:
            pytest.fail("Organization ID is required for schedule_report")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.post(
            f"/api/reports/{report_id}/schedule",
            json={"cron_expression": cron_expression},
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