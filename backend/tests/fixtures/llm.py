import pytest
import os


@pytest.fixture
def create_azure_provider_and_models(test_client):
    def _create_azure_provider_and_models(user_token=None, org_id=None, base_url=None):
        azure_api_key = os.getenv("AZURE_API_KEY_TEST", "")
        azure_endpoint = os.getenv("AZURE_ENDPOINT_TEST", "")

        if not azure_api_key:
            pytest.skip("AZURE_API_KEY_TEST is not set")

        if not azure_endpoint:
            pytest.skip("AZURE_ENDPOINT_TEST is not set")
        headers = {}
        if user_token:
            headers["Authorization"] = f"Bearer {user_token}"
        if org_id:
            headers["X-Organization-Id"] = str(org_id)

        credentials = {
            "api_key": str(azure_api_key),
            "endpoint_url": azure_endpoint
        }
        
        response = test_client.post(
            "/api/llm/providers",
            json={"name": 'azure provider',
                   "provider_type": "azure",
                   "credentials": credentials,
                   "models": [
                    {
                        "model_id": "gpt-5.4",
                        "name": "GPT-5.4",
                        "is_custom": False,
                        "is_default": True
                    }
                   ]},
            headers=headers
        )
        return response.json()
    
    return _create_azure_provider_and_models


@pytest.fixture
def create_bedrock_provider_and_models(test_client):
    def _create_bedrock_provider_and_models(user_token=None, org_id=None):
        region = os.getenv("AWS_BEDROCK_REGION", "us-east-1")

        headers = {}
        if user_token:
            headers["Authorization"] = f"Bearer {user_token}"
        if org_id:
            headers["X-Organization-Id"] = str(org_id)

        credentials = {
            "region": region,
            "auth_mode": "iam",
        }

        response = test_client.post(
            "/api/llm/providers",
            json={
                "name": "bedrock provider",
                "provider_type": "bedrock",
                "credentials": credentials,
                "models": [
                    {
                        "model_id": os.getenv(
                            "AWS_BEDROCK_MODEL_ID",
                            "us.anthropic.claude-3-5-haiku-20241022-v1:0",
                        ),
                        "name": "Claude 3.5 Haiku (Bedrock)",
                        "is_custom": True,
                        "is_default": True,
                    }
                ],
            },
            headers=headers,
        )
        return response.json()

    return _create_bedrock_provider_and_models


@pytest.fixture
def create_anthropic_provider_and_models(test_client):
    """Create an Anthropic provider + Claude 4.6 Sonnet and Haiku models.

    Uses ``ANTHROPIC_API_KEY_TEST`` from env. Sets Sonnet as default and
    Haiku as the small default so the judge and planner use sensible
    models. Fails the test if the key is not set.
    """
    def _create(user_token=None, org_id=None):
        anthropic_api_key = os.getenv("ANTHROPIC_API_KEY_TEST", "")
        if not anthropic_api_key:
            pytest.fail("ANTHROPIC_API_KEY_TEST is not set")
        headers = {}
        if user_token:
            headers["Authorization"] = f"Bearer {user_token}"
        if org_id:
            headers["X-Organization-Id"] = str(org_id)

        response = test_client.post(
            "/api/llm/providers",
            json={
                "name": "anthropic provider",
                "provider_type": "anthropic",
                "credentials": {"api_key": str(anthropic_api_key)},
                "models": [
                    {
                        "model_id": "claude-sonnet-4-6",
                        "name": "Claude 4.6 Sonnet",
                        "is_custom": False,
                    },
                    {
                        "model_id": "claude-haiku-4-5-20251001",
                        "name": "Claude 4.5 Haiku",
                        "is_custom": False,
                    },
                ],
            },
            headers=headers,
        )
        return response.json()

    return _create


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
                           "model_id": "gpt-5.4",
                           "name": "GPT-5.4",
                           "is_custom": False
                       },
                       {
                           "model_id": "gpt-5.4-mini",
                           "name": "GPT-5.4 Mini",
                           "is_custom": False
                       },
                       {
                           "model_id": "gpt-5.2",
                           "name": "GPT-5.2",
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
                           "model_id": "gpt-5.4",
                           "name": "GPT-5.4",
                           "is_custom": False
                       },
                       {
                           "model_id": "gpt-5.4-mini",
                           "name": "GPT-5.4 Mini",
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