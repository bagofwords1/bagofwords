import pytest

@pytest.fixture
def create_completion(test_client):
    def _create_completion(*, report_id: str, prompt: str, user_token: str = None, org_id: str = None):
        if user_token is None:
            pytest.fail("User token is required for create_completion")
        if org_id is None:
            pytest.fail("Organization ID is required for create_completion")
        
        payload = {
            "prompt": {
                "content": prompt,
                "widget_id": None,
                "step_id": None,
                "mentions": [{}]
            }
        }
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.post(
            f"/api/reports/{report_id}/completions",
            json=payload,
            headers=headers,
            params={"background": False}
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _create_completion

@pytest.fixture
def get_completions(test_client):
    def _get_completions(*, report_id: str, user_token: str = None, org_id: str = None):
        if user_token is None:
            pytest.fail("User token is required for get_completions")
        if org_id is None:
            pytest.fail("Organization ID is required for get_completions")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.get(
            f"/api/reports/{report_id}/completions",
            headers=headers
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _get_completions
