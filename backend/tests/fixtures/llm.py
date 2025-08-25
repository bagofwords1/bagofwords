import pytest
import os

@pytest.fixture
def create_llm_provider_and_models(test_client):
    def _create_llm_provider_and_models(user_token=None, org_id=None, base_url=None):
        openai_api_key = os.getenv("OPENAI_API_KEY_TEST", "")

        if not openai_api_key:
            pytest.fail("OPENAI_API_KEY_TEST is not set")
        headers = {}
        if user_token:
            headers["Authorization"] = f"Bearer {user_token}"
        if org_id:
            headers["X-Organization-Id"] = str(org_id)

        # Build credentials with optional base_url
        credentials = {"api_key": str(openai_api_key)}
        if base_url:
            credentials["base_url"] = base_url

        response = test_client.post(
            "/api/llm/providers",
            json={"name": 'openai provider',
                   "provider_type": "openai",
                   "credentials": credentials,
                   "models": [
                       {
                           "model_id": "gpt-4.1",
                           "name": "GPT-4.1",
                           "is_custom": False
                       },
                       {
                           "model_id": "gpt-4.1-mini",
                           "name": "GPT-4.1 Mini",
                           "is_custom": False
                       },

                       {
                           "model_id": "gpt-4o",
                           "name": "GPT-4o",
                           "is_custom": False
                       },
                       {
                           "model_id": "gpt-4o-mini",
                           "name": "GPT-4o Mini",
                           "is_custom": False
                       },
                       {
                           "model_id": "o1",
                           "name": "o1",
                           "is_custom": False
                       },
                       {
                           "model_id": "o1-mini",
                           "name": "o1 Mini",
                           "is_custom": False
                       }
                       ]},
            headers=headers
        )
        return response.json()
    
    return _create_llm_provider_and_models

@pytest.fixture
def create_openai_provider_with_base_url(test_client):
    def _create_openai_provider_with_base_url(base_url, user_token=None, org_id=None, provider_name=None):
        openai_api_key = os.getenv("OPENAI_API_KEY_TEST", "")

        if not openai_api_key:
            pytest.fail("OPENAI_API_KEY_TEST is not set")
        
        headers = {}
        if user_token:
            headers["Authorization"] = f"Bearer {user_token}"
        if org_id:
            headers["X-Organization-Id"] = str(org_id)

        response = test_client.post(
            "/api/llm/providers",
            json={
                "name": provider_name or 'openai provider with custom base_url',
                "provider_type": "openai",
                "credentials": {
                    "api_key": str(openai_api_key),
                    "base_url": base_url
                },
                "models": [
                       {
                           "model_id": "gpt-4.1",
                           "name": "GPT-4.1",
                           "is_custom": False
                       },
                       {
                           "model_id": "gpt-4.1-mini",
                           "name": "GPT-4.1 Mini",
                           "is_custom": False
                       }
                ]
            },
            headers=headers
        )
        return response.json()
    
    return _create_openai_provider_with_base_url

@pytest.fixture
def update_llm_provider_base_url(test_client):
    def _update_llm_provider_base_url(provider_id, base_url, user_token=None, org_id=None):
        headers = {}
        if user_token:
            headers["Authorization"] = f"Bearer {user_token}"
        if org_id:
            headers["X-Organization-Id"] = str(org_id)

        # Build update payload - base_url can be set to None to clear it
        credentials = {}
        if base_url is not None:
            credentials["base_url"] = base_url
        else:
            credentials["base_url"] = ""  # Empty string to clear base_url

        response = test_client.put(
            f"/api/llm/providers/{provider_id}",
            json={
                "credentials": credentials,
                "models": []  # Empty models array since we're just updating base_url
            },
            headers=headers
        )
        return response.json()
    
    return _update_llm_provider_base_url

@pytest.fixture
def get_models(test_client):
    def _get_models(user_token=None, org_id=None):
        response = test_client.get(
            "/api/llm/models",
            headers={"Authorization": f"Bearer {user_token}", "X-Organization-Id": org_id}
        )
        return response.json()
    
    return _get_models

@pytest.fixture
def get_default_model(test_client):
    def _get_default_model(user_token=None, org_id=None):
        response = test_client.get(
            "/api/llm/models",
            headers={"Authorization": f"Bearer {user_token}", "X-Organization-Id": org_id}
        )
        return [model for model in response.json() if model['is_default']]
    
    return _get_default_model

@pytest.fixture
def set_llm_provider_as_default(test_client):
    def _set_llm_provider_as_default(provider_id, user_token=None, org_id=None):
        response = test_client.post(
            f"/api/llm/providers/{provider_id}/set_default",
            headers={"Authorization": f"Bearer {user_token}", "X-Organization-Id": org_id}
        )
        return response.json()
    
    return _set_llm_provider_as_default

@pytest.fixture
def toggle_llm_active_status(test_client):
    def _toggle_llm_active_status(llm_id, enabled, user_token=None, org_id=None):
        response = test_client.post(
            f"/api/llm/models/{llm_id}/toggle?enabled={enabled}",
            headers={"Authorization": f"Bearer {user_token}", "X-Organization-Id": org_id}
        )
        return response.json()
    
    return _toggle_llm_active_status

@pytest.fixture
def delete_llm_provider(test_client):
    def _delete_llm_provider(provider_id, user_token=None, org_id=None):
        response = test_client.delete(
            f"/api/llm/providers/{provider_id}",
            headers={"Authorization": f"Bearer {user_token}", "X-Organization-Id": org_id}
        )
        return response.json()
    
    return _delete_llm_provider