import pytest

@pytest.fixture
def create_data_source(test_client):
    def _create_data_source(*, name: str, type: str, config: dict = None, credentials: dict = None, user_token: str = None, org_id: str = None):
        if user_token is None:
            pytest.fail("User token is required for create_data_source")
        if org_id is None:
            pytest.fail("Organization ID is required for create_data_source")
        
        payload = {
            "name": name,
            "type": type,
            "config": config or {},
            "credentials": credentials or {},
            "generate_summary": False,
            "generate_conversation_starters": False,
            "generate_ai_rules": False
        }
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.post(
            "/api/data_sources",
            json=payload,
            headers=headers
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _create_data_source

@pytest.fixture
def get_data_sources(test_client):
    def _get_data_sources(*, user_token: str = None, org_id: str = None):
        if user_token is None:
            pytest.fail("User token is required for get_data_sources")
        if org_id is None:
            pytest.fail("Organization ID is required for get_data_sources")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.get(
            "/api/data_sources",
            headers=headers
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _get_data_sources


@pytest.fixture
def test_connection(test_client):
    def _test_connection(*, data_source_id: str, user_token: str = None, org_id: str = None):
        if user_token is None:
            pytest.fail("User token is required for test_connection")
        if org_id is None:
            pytest.fail("Organization ID is required for test_connection")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }

        response = test_client.get(
            f"/api/data_sources/{data_source_id}/test_connection",
            headers=headers
        )

        assert response.status_code == 200, response.json()
        return response.json()
