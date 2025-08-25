import pytest
from fastapi.testclient import TestClient
from main import app
from tests.utils.user_creds import main_user

@pytest.mark.e2e
def test_llm_providers(create_llm_provider_and_models, get_models, get_default_model, set_llm_provider_as_default, toggle_llm_active_status, delete_llm_provider, create_user, login_user, get_user_organizations, create_organization):
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = create_organization(user_token=user_token)

    provider_id = create_llm_provider_and_models(user_token, org_id)
    models = get_models(user_token, org_id)
    
    assert len(models) > 0

    # should have one default model
    default_model = get_default_model(user_token, org_id)
    
    assert len(default_model) == 1

    #set_llm_provider_as_default(provider_id, user_token, org_id)
    #toggle_llm_active_status(default_model[0]["id"], True, user_token, org_id)
    #delete_llm_provider(provider_id, user_token, org_id)

@pytest.mark.e2e
def test_llm_provider_with_base_url(create_openai_provider_with_base_url, get_models, create_user, login_user, create_organization, test_client):
    """Test creating an OpenAI provider with a custom base_url"""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = create_organization(user_token=user_token)

    # Test with a custom base URL (e.g., for OpenAI-compatible services)
    custom_base_url = "https://api.groq.com/openai/v1"
    
    provider_response = create_openai_provider_with_base_url(
        base_url=custom_base_url,
        user_token=user_token,
        org_id=org_id,
        provider_name="Custom OpenAI Provider"
    )
    
    assert "id" in provider_response
    provider_id = provider_response["id"]
    
    # Verify the provider was created successfully
    response = test_client.get(
        "/api/llm/providers",
        headers={"Authorization": f"Bearer {user_token}", "X-Organization-Id": org_id}
    )
    
    providers = response.json()
    custom_provider = next((p for p in providers if p["id"] == provider_id), None)
    
    assert custom_provider is not None
    assert custom_provider["name"] == "Custom OpenAI Provider"
    assert custom_provider["provider_type"] == "openai"
    # Check that base_url is stored in additional_config
    assert custom_provider["additional_config"] is not None
    assert custom_provider["additional_config"]["base_url"] == custom_base_url
    
    # Verify models were created
    models = get_models(user_token, org_id)
    provider_models = [m for m in models if m["provider_id"] == provider_id]
    assert len(provider_models) > 0

@pytest.mark.e2e
def test_llm_provider_update_base_url(create_llm_provider_and_models, update_llm_provider_base_url, create_user, login_user, create_organization, test_client):
    """Test updating a provider's base_url"""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = create_organization(user_token=user_token)

    # Create a standard OpenAI provider without base_url
    provider_response = create_llm_provider_and_models(user_token, org_id)
    provider_id = provider_response["id"]
    
    # Verify initially no base_url
    response = test_client.get(
        "/api/llm/providers",
        headers={"Authorization": f"Bearer {user_token}", "X-Organization-Id": org_id}
    )
    providers = response.json()
    provider = next((p for p in providers if p["id"] == provider_id), None)
    assert provider["additional_config"] is None or "base_url" not in provider["additional_config"]
    
    # Update to add a base_url
    new_base_url = "https://api.openai-compatible.com/v1"
    update_response = update_llm_provider_base_url(
        provider_id=provider_id,
        base_url=new_base_url,
        user_token=user_token,
        org_id=org_id
    )
    
    # Verify the update was successful
    response = test_client.get(
        "/api/llm/providers",
        headers={"Authorization": f"Bearer {user_token}", "X-Organization-Id": org_id}
    )
    providers = response.json()
    updated_provider = next((p for p in providers if p["id"] == provider_id), None)
    
    assert updated_provider["additional_config"] is not None
    assert updated_provider["additional_config"]["base_url"] == new_base_url

@pytest.mark.e2e
def test_llm_provider_clear_base_url(create_openai_provider_with_base_url, update_llm_provider_base_url, create_user, login_user, create_organization, test_client):
    """Test clearing a provider's base_url"""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = create_organization(user_token=user_token)

    # Create a provider with base_url
    initial_base_url = "https://api.custom.com/v1"
    provider_response = create_openai_provider_with_base_url(
        base_url=initial_base_url,
        user_token=user_token,
        org_id=org_id
    )
    provider_id = provider_response["id"]
    
    # Verify base_url exists
    response = test_client.get(
        "/api/llm/providers",
        headers={"Authorization": f"Bearer {user_token}", "X-Organization-Id": org_id}
    )
    providers = response.json()
    provider = next((p for p in providers if p["id"] == provider_id), None)
    assert provider["additional_config"]["base_url"] == initial_base_url
    
    # Clear the base_url by passing None
    update_response = update_llm_provider_base_url(
        provider_id=provider_id,
        base_url=None,  # This should clear the base_url
        user_token=user_token,
        org_id=org_id
    )
    
    # Verify base_url was cleared
    response = test_client.get(
        "/api/llm/providers",
        headers={"Authorization": f"Bearer {user_token}", "X-Organization-Id": org_id}
    )
    providers = response.json()
    updated_provider = next((p for p in providers if p["id"] == provider_id), None)
    
    # base_url should be removed from additional_config
    assert updated_provider["additional_config"] is None or "base_url" not in updated_provider["additional_config"]

@pytest.mark.e2e
def test_llm_provider_with_base_url_creates_models(create_openai_provider_with_base_url, get_models, create_user, login_user, create_organization):
    """Test that creating a provider with base_url still creates models correctly"""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = create_organization(user_token=user_token)

    # Create provider with custom base_url
    provider_response = create_openai_provider_with_base_url(
        base_url="https://api.perplexity.ai",
        user_token=user_token,
        org_id=org_id
    )
    provider_id = provider_response["id"]
    
    # Get all models for the organization
    models = get_models(user_token, org_id)
    
    # Filter models for this provider
    provider_models = [m for m in models if m["provider_id"] == provider_id]
    
    # Should have created the expected models
    assert len(provider_models) >= 2  # At least gpt-4.1 and gpt-4.1-mini based on the fixture
    
    # Verify model details
    model_ids = [m["model_id"] for m in provider_models]
    assert "gpt-4.1" in model_ids
    assert "gpt-4.1-mini" in model_ids
    
    # All models should be enabled by default
    for model in provider_models:
        assert model["is_enabled"] == True