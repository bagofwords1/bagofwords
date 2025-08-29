import pytest

@pytest.fixture
def create_completion(test_client):
    def _create_completion(*, report_id: str, prompt: str, user_token: str = None, org_id: str = None, background: bool = False):
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
        
        params = {"background": background}
        response = test_client.post(
            f"/api/reports/{report_id}/completions",
            json=payload,
            headers=headers,
            params=params
        )
        
        assert response.status_code == 200, response.json()
        data = response.json()
        # CompletionsV2Response shape
        if isinstance(data, dict) and "completions" in data:
            return data["completions"]
        return data
    
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
        data = response.json()
        # New v2 endpoint returns an object with completions list
        if isinstance(data, dict) and "completions" in data:
            return data["completions"]
        return data
    
    return _get_completions

@pytest.fixture
def create_completion_stream(test_client):
    def _create_completion_stream(*, report_id: str, prompt: str, user_token: str = None, org_id: str = None):
        if user_token is None:
            pytest.fail("User token is required for create_completion_stream")
        if org_id is None:
            pytest.fail("Organization ID is required for create_completion_stream")

        payload = {
            "prompt": {
                "content": prompt,
                "widget_id": None,
                "step_id": None,
                "mentions": [{}]
            },
            "stream": True
        }

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
            "Accept": "text/event-stream"
        }

        with test_client.stream(
            "POST",
            f"/api/reports/{report_id}/completions",
            json=payload,
            headers=headers,
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers.get("content-type", "").startswith("text/event-stream")
            # Return the streaming response iterator to the test
            return resp.iter_lines()

    return _create_completion_stream
