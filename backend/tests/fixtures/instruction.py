import pytest
from fastapi.testclient import TestClient
from main import app
import os

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
def create_global_instruction(test_client):
    def _create_global_instruction(text="Test Instruction", user_token=None, org_id=None, status="draft", category="general", data_source_ids=None, label_ids=None):
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
        if label_ids is not None:
            payload["label_ids"] = label_ids
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.post(
            "/api/instructions/global",
            json=payload,
            headers=headers
        )
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _create_global_instruction

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
    def _update_instruction(instruction_id, text=None, status=None, category=None, data_source_ids=None, label_ids=None, user_token=None, org_id=None):
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
        if label_ids is not None:
            payload["label_ids"] = label_ids
        
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

@pytest.fixture
def create_label(test_client):
    def _create_label(name="Test Label", color="#34d399", description=None, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for create_label")
        if org_id is None:
            pytest.fail("Organization ID is required for create_label")
        
        payload = {
            "name": name,
            "color": color,
        }
        if description is not None:
            payload["description"] = description
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.post(
            "/api/instructions/labels",
            json=payload,
            headers=headers
        )
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _create_label

@pytest.fixture
def list_labels(test_client):
    def _list_labels(user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for list_labels")
        if org_id is None:
            pytest.fail("Organization ID is required for list_labels")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.get(
            "/api/instructions/labels",
            headers=headers
        )
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _list_labels

@pytest.fixture
def update_label(test_client):
    def _update_label(label_id, name=None, color=None, description=None, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for update_label")
        if org_id is None:
            pytest.fail("Organization ID is required for update_label")
        
        payload = {}
        if name is not None:
            payload["name"] = name
        if color is not None:
            payload["color"] = color
        if description is not None:
            payload["description"] = description
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.patch(
            f"/api/instructions/labels/{label_id}",
            json=payload,
            headers=headers
        )
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _update_label

@pytest.fixture
def delete_label(test_client):
    def _delete_label(label_id, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for delete_label")
        if org_id is None:
            pytest.fail("Organization ID is required for delete_label")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.delete(
            f"/api/instructions/labels/{label_id}",
            headers=headers
        )
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _delete_label