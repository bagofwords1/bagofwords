import pytest
from fastapi.testclient import TestClient
from main import app
from tests.utils.user_creds import main_user

@pytest.mark.e2e
def test_llm_providers(create_llm_provider_and_models, get_models, set_llm_provider_as_default, toggle_llm_active_status, delete_llm_provider, create_user, login_user, get_user_organizations, create_organization):
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = create_organization(user_token=user_token)

    provider_id = create_llm_provider_and_models(user_token, org_id)
    models = get_models(user_token, org_id)
    
    assert len(models) > 0

    # should have one default model
    default_model = [model for model in models if model['is_default']]
    assert len(default_model) == 1

    #set_llm_provider_as_default(provider_id, user_token, org_id)
    #toggle_llm_active_status(default_model[0]["id"], True, user_token, org_id)
    #delete_llm_provider(provider_id, user_token, org_id)