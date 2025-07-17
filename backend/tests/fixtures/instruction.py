import pytest

@pytest.fixture
def create_instruction(test_client):
    def _create_instruction(text="Test Instruction", user_token=None, org_id=None, status="draft", category="general", data_source_ids=None):
        if user_token is None:
            pytest.fail("User token is required for create_instruction")
        if org_id is None:
            pytest.fail("Organization ID is required for create_instruction")
        
        payload = {
            "text": text,
            "status": status,
            "category": category,
            "data_source_ids": data_source_ids or []
        }
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.post(
            "/api/instructions",
            json=payload,
            headers=headers
        )
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _create_instruction

@pytest.fixture
def get_instructions(test_client):
    def _get_instructions(user_token=None, org_id=None, status=None, category=None, data_source_id=None):
        if user_token is None:
            pytest.fail("User token is required for get_instructions")
        if org_id is None:
            pytest.fail("Organization ID is required for get_instructions")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        params = {}
        if status:
            params["status"] = status
        if category:
            params["category"] = category
        if data_source_id:
            params["data_source_id"] = data_source_id
        
        response = test_client.get(
            "/api/instructions",
            headers=headers,
            params=params
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _get_instructions

@pytest.fixture
def get_instruction(test_client):
    def _get_instruction(instruction_id, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for get_instruction")
        if org_id is None:
            pytest.fail("Organization ID is required for get_instruction")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.get(
            f"/api/instructions/{instruction_id}",
            headers=headers
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _get_instruction

@pytest.fixture
def update_instruction(test_client):
    def _update_instruction(instruction_id, text=None, status=None, category=None, data_source_ids=None, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for update_instruction")
        if org_id is None:
            pytest.fail("Organization ID is required for update_instruction")
        
        payload = {}
        if text is not None:
            payload["text"] = text
        if status is not None:
            payload["status"] = status
        if category is not None:
            payload["category"] = category
        if data_source_ids is not None:
            payload["data_source_ids"] = data_source_ids
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.put(
            f"/api/instructions/{instruction_id}",
            json=payload,
            headers=headers
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _update_instruction

@pytest.fixture
def delete_instruction(test_client):
    def _delete_instruction(instruction_id, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for delete_instruction")
        if org_id is None:
            pytest.fail("Organization ID is required for delete_instruction")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.delete(
            f"/api/instructions/{instruction_id}",
            headers=headers
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _delete_instruction

@pytest.fixture
def get_instructions_for_data_source(test_client):
    def _get_instructions_for_data_source(data_source_id, user_token=None, org_id=None, status="published"):
        if user_token is None:
            pytest.fail("User token is required for get_instructions_for_data_source")
        if org_id is None:
            pytest.fail("Organization ID is required for get_instructions_for_data_source")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        params = {"status": status}
        
        response = test_client.get(
            f"/api/data_sources/{data_source_id}/instructions",
            headers=headers,
            params=params
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _get_instructions_for_data_source

@pytest.fixture
def get_instruction_categories(test_client):
    def _get_instruction_categories(user_token=None, org_id=None):
        headers = {}
        if user_token and org_id:
            headers = {
                "Authorization": f"Bearer {user_token}",
                "X-Organization-Id": str(org_id)
            }
        
        response = test_client.get("/api/instructions/categories", headers=headers)
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _get_instruction_categories

@pytest.fixture
def get_instruction_statuses(test_client):
    def _get_instruction_statuses(user_token=None, org_id=None):
        headers = {}
        if user_token and org_id:
            headers = {
                "Authorization": f"Bearer {user_token}",
                "X-Organization-Id": str(org_id)
            }
        
        response = test_client.get("/api/instructions/statuses", headers=headers)
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _get_instruction_statuses

@pytest.fixture
def increment_thumbs_up(test_client):
    def _increment_thumbs_up(instruction_id, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for increment_thumbs_up")
        if org_id is None:
            pytest.fail("Organization ID is required for increment_thumbs_up")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.post(
            f"/api/instructions/{instruction_id}/thumbs-up",
            headers=headers
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _increment_thumbs_up