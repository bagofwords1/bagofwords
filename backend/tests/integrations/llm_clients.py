"""
LLM Client Integration Tests

Tests connectivity and basic inference for LLM provider clients.
Run locally: pytest backend/tests/integrations/llm_clients.py -v
Run specific: pytest backend/tests/integrations/llm_clients.py -k "openai" -v
"""
import os
import json
import pytest
import logging
from typing import Dict, Any

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# =============================================================================
# SOURCE OF TRUTH: LLM providers to test
# =============================================================================
LLM_PROVIDERS = [
    "openai",
    "anthropic",
    "google",
    "azure",
]

# Test prompt for all providers
TEST_PROMPT = "What is 2 + 2? Reply with just the number."


# =============================================================================
# Credentials Loading
# =============================================================================
def load_credentials() -> Dict[str, Any]:
    """Load credentials from integrations.json in the tests folder."""
    credentials_path = os.path.join(os.path.dirname(__file__), "integrations.json")
    if not os.path.exists(credentials_path):
        return {}
    with open(credentials_path, "r") as file:
        return json.load(file)


CREDENTIALS: Dict[str, Any] = load_credentials()
LLM_CREDENTIALS: Dict[str, Any] = CREDENTIALS.get("llms", {})


def llm_kwargs(name: str) -> Dict[str, Any]:
    """
    Extract kwargs for an LLM provider from credentials.
    Skips the test if the provider is missing or disabled.
    """
    cfg = dict(LLM_CREDENTIALS.get(name, {}))
    if not cfg:
        pytest.skip(f"{name} missing in integrations.json (llms)")
    
    enabled = cfg.pop("enabled", False)
    if not enabled:
        pytest.skip(f"{name} disabled in integrations.json")

    return cfg


# =============================================================================
# Client Factory
# =============================================================================
def get_llm_client(provider: str, **kwargs):
    """
    Instantiate an LLM client by provider name.
    """
    if provider == "openai":
        from app.ai.llm.clients.openai_client import OpenAi
        return OpenAi(
            api_key=kwargs["api_key"],
            base_url=kwargs.get("base_url", "https://api.openai.com/v1"),
        )
    
    elif provider == "anthropic":
        from app.ai.llm.clients.anthropic_client import Anthropic
        return Anthropic(
            api_key=kwargs["api_key"],
        )
    
    elif provider == "google":
        from app.ai.llm.clients.google_client import Google
        return Google(
            api_key=kwargs["api_key"],
        )
    
    elif provider == "azure":
        from app.ai.llm.clients.azure_client import AzureClient
        return AzureClient(
            api_key=kwargs["api_key"],
            endpoint_url=kwargs["endpoint_url"],
            api_version=kwargs.get("api_version"),
        )
    
    else:
        raise ValueError(f"Unknown LLM provider: {provider}")


# =============================================================================
# Parametrized Integration Test
# =============================================================================
@pytest.mark.parametrize("provider", LLM_PROVIDERS)
def test_llm_inference(provider: str) -> None:
    """
    Test basic inference for an LLM provider.
    
    1. Instantiate the client
    2. Run a simple inference
    3. Verify we get a response
    """
    cfg = llm_kwargs(provider)
    model_id = cfg.pop("model_id", None)
    
    if not model_id:
        pytest.skip(f"{provider}: no model_id configured")
    
    client = get_llm_client(provider, **cfg)
    
    logger.info(f"{provider}: Testing inference with model {model_id}...")
    
    response = client.inference(model_id=model_id, prompt=TEST_PROMPT)
    
    assert response is not None, f"{provider}: Got None response"
    assert response.text, f"{provider}: Got empty response text"
    
    logger.info(f"{provider}: Response: {response.text[:100]}")
    logger.info(f"{provider}: Usage: {response.usage}")
    
    # Basic sanity check - response should contain "4"
    assert "4" in response.text, f"{provider}: Expected '4' in response, got: {response.text}"
    
    logger.info(f"{provider}: Inference successful")


@pytest.mark.parametrize("provider", LLM_PROVIDERS)
@pytest.mark.asyncio
async def test_llm_inference_stream(provider: str) -> None:
    """
    Test streaming inference for an LLM provider.
    
    1. Instantiate the client
    2. Run streaming inference
    3. Collect all chunks
    4. Verify we get a response
    """
    cfg = llm_kwargs(provider)
    model_id = cfg.pop("model_id", None)
    
    if not model_id:
        pytest.skip(f"{provider}: no model_id configured")
    
    client = get_llm_client(provider, **cfg)
    
    logger.info(f"{provider}: Testing streaming inference with model {model_id}...")
    
    chunks = []
    async for chunk in client.inference_stream(model_id=model_id, prompt=TEST_PROMPT):
        chunks.append(chunk)
    
    full_response = "".join(chunks)
    
    assert full_response, f"{provider}: Got empty streaming response"
    
    logger.info(f"{provider}: Streamed response: {full_response[:100]}")
    
    # Basic sanity check
    assert "4" in full_response, f"{provider}: Expected '4' in response, got: {full_response}"
    
    logger.info(f"{provider}: Streaming inference successful")

