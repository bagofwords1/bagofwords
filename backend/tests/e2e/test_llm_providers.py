import asyncio

import pytest

from app.dependencies import async_session_maker
from app.models.llm_model import LLMModel
from app.models.llm_provider import LLMProvider


def _run(coro):
    return asyncio.run(coro)


async def _seed_stale_openai_preset(org_id):
    """Seed a stale preset provider; the public API only creates custom providers."""
    async with async_session_maker() as db:
        provider = LLMProvider(
            organization_id=org_id,
            name="OpenAI",
            provider_type="openai",
            is_preset=True,
            is_enabled=True,
            use_preset_credentials=True,
        )
        db.add(provider)
        await db.flush()

        db.add_all([
            LLMModel(
                organization_id=org_id,
                provider_id=provider.id,
                name="GPT-5.5",
                model_id="gpt-5.5",
                is_preset=True,
                is_enabled=True,
                is_default=True,
                supports_vision=True,
            ),
            LLMModel(
                organization_id=org_id,
                provider_id=provider.id,
                name="GPT-5.4 Mini",
                model_id="gpt-5.4-mini",
                is_preset=True,
                is_enabled=True,
                is_small_default=True,
                supports_vision=True,
            ),
        ])
        await db.commit()
        return str(provider.id)


@pytest.mark.e2e
def test_llm_providers(create_llm_provider_and_models, get_models, get_default_model, create_user, login_user, whoami):
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    provider_id = create_llm_provider_and_models(user_token, org_id)
    models = get_models(user_token, org_id)
    
    assert len(models) > 0

    # should have one default model
    default_model = get_default_model(user_token, org_id)
    
    assert len(default_model) == 1
    assert default_model[0]["model_id"] == "gpt-5.6-terra"

    #set_llm_provider_as_default(provider_id, user_token, org_id)
    #toggle_llm_active_status(default_model[0]["id"], True, user_token, org_id)
    #delete_llm_provider(provider_id, user_token, org_id)

@pytest.mark.e2e
def test_llm_provider_with_base_url(create_openai_provider_with_base_url, get_models, create_user, login_user, whoami, test_client):
    """Test creating an OpenAI provider with a custom base_url"""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

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
def test_llm_provider_update_base_url(create_llm_provider_and_models, update_llm_provider_base_url, create_user, login_user, whoami, test_client):
    """Test updating a provider's base_url"""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

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
def test_llm_provider_clear_base_url(create_openai_provider_with_base_url, update_llm_provider_base_url, create_user, login_user, whoami, test_client):
    """Test clearing a provider's base_url"""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

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
def test_llm_provider_with_base_url_creates_models(create_openai_provider_with_base_url, get_models, create_user, login_user, whoami):
    """Test that creating a provider with base_url still creates models correctly"""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

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
    assert len(provider_models) >= 2  # At least Sol and Luna based on the fixture

    # Verify model details
    model_ids = [m["model_id"] for m in provider_models]
    assert "gpt-5.6-sol" in model_ids
    assert "gpt-5.6-luna" in model_ids
    
    # All models should be enabled by default
    for model in provider_models:
        assert model["is_enabled"] == True


@pytest.mark.e2e
def test_preset_openai_sync_migrates_to_gpt_56_models(test_client, create_user, login_user, whoami):
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    provider_id = _run(_seed_stale_openai_preset(org_id))

    response = test_client.get(
        "/api/llm/models",
        headers={"Authorization": f"Bearer {user_token}", "X-Organization-Id": org_id},
    )
    assert response.status_code == 200, response.json()

    provider_models = [m for m in response.json() if m["provider_id"] == provider_id]
    by_model_id = {m["model_id"]: m for m in provider_models}

    assert by_model_id["gpt-5.6-sol"]["is_enabled"] is True
    assert by_model_id["gpt-5.6-sol"]["is_default"] is False
    assert by_model_id["gpt-5.6-sol"]["max_output_tokens"] == 128000

    assert by_model_id["gpt-5.6-terra"]["is_enabled"] is True
    assert by_model_id["gpt-5.6-terra"]["is_default"] is True

    assert by_model_id["gpt-5.6-luna"]["is_enabled"] is True
    assert by_model_id["gpt-5.6-luna"]["is_small_default"] is True

    assert by_model_id["gpt-5.5"]["is_enabled"] is True
    assert by_model_id["gpt-5.5"]["is_default"] is False
    assert by_model_id["gpt-5.5"]["max_output_tokens"] == 128000
    assert by_model_id["gpt-5.4-mini"]["is_enabled"] is False
    assert by_model_id["gpt-5.4-mini"]["is_small_default"] is False
