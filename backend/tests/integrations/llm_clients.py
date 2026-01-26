"""
LLM Client Integration Tests

Tests connectivity and basic inference for LLM provider clients.
Run locally: pytest backend/tests/integrations/llm_clients.py -v
Run specific: pytest backend/tests/integrations/llm_clients.py -k "openai" -v
Run vision tests: pytest backend/tests/integrations/llm_clients.py -k "vision" -v
"""
import os
import json
import pytest
import logging
from typing import Dict, Any

from app.ai.llm.types import ImageInput
from app.models.llm_model import LLM_MODEL_DETAILS

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

# Vision-capable providers
VISION_PROVIDERS = [
    "openai",
    "anthropic",
    "google",
    "azure",
]

# Test prompt for vision tests
VISION_TEST_PROMPT = "What text is shown in this image? Reply with just the text."

# Path to test image (relative to this file)
TEST_IMAGE_PATH = os.path.join(os.path.dirname(__file__), "test_image.png")


def load_test_image() -> ImageInput:
    """Load the test image as base64."""
    import base64
    with open(TEST_IMAGE_PATH, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")
    return ImageInput(data=image_data, media_type="image/png", source_type="base64")


def model_supports_vision(model_id: str) -> bool:
    """Check if a model supports vision based on LLM_MODEL_DETAILS."""
    for detail in LLM_MODEL_DETAILS:
        if detail.get("model_id") == model_id:
            return detail.get("supports_vision", False)
    return False


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


# =============================================================================
# Vision Integration Tests
# =============================================================================
@pytest.mark.parametrize("provider", VISION_PROVIDERS)
def test_llm_vision_inference(provider: str) -> None:
    """
    Test vision inference for an LLM provider.

    1. Instantiate the client
    2. Run inference with an image
    3. Verify we get a response about the image content
    """
    cfg = llm_kwargs(provider)
    model_id = cfg.pop("model_id", None)

    if not model_id:
        pytest.skip(f"{provider}: no model_id configured")

    if not model_supports_vision(model_id):
        pytest.skip(f"{provider}: model {model_id} does not support vision")

    if not os.path.exists(TEST_IMAGE_PATH):
        pytest.skip("Test image not found")

    client = get_llm_client(provider, **cfg)
    image = load_test_image()

    logger.info(f"{provider}: Testing vision inference with model {model_id}...")

    response = client.inference(model_id=model_id, prompt=VISION_TEST_PROMPT, images=[image])

    assert response is not None, f"{provider}: Got None response"
    assert response.text, f"{provider}: Got empty response text"

    logger.info(f"{provider}: Vision response: {response.text[:100]}")
    logger.info(f"{provider}: Usage: {response.usage}")

    # The test image shows "BOW" text
    assert "BOW" in response.text.upper(), f"{provider}: Expected 'BOW' in response, got: {response.text}"

    logger.info(f"{provider}: Vision inference successful")


@pytest.mark.parametrize("provider", VISION_PROVIDERS)
@pytest.mark.asyncio
async def test_llm_vision_inference_stream(provider: str) -> None:
    """
    Test streaming vision inference for an LLM provider.

    1. Instantiate the client
    2. Run streaming inference with an image
    3. Collect all chunks
    4. Verify we get a response about the image content
    """
    cfg = llm_kwargs(provider)
    model_id = cfg.pop("model_id", None)

    if not model_id:
        pytest.skip(f"{provider}: no model_id configured")

    if not model_supports_vision(model_id):
        pytest.skip(f"{provider}: model {model_id} does not support vision")

    if not os.path.exists(TEST_IMAGE_PATH):
        pytest.skip("Test image not found")

    client = get_llm_client(provider, **cfg)
    image = load_test_image()

    logger.info(f"{provider}: Testing streaming vision inference with model {model_id}...")

    chunks = []
    async for chunk in client.inference_stream(model_id=model_id, prompt=VISION_TEST_PROMPT, images=[image]):
        chunks.append(chunk)

    full_response = "".join(chunks)

    assert full_response, f"{provider}: Got empty streaming response"

    logger.info(f"{provider}: Streamed vision response: {full_response[:100]}")

    # The test image shows "BOW" text
    assert "BOW" in full_response.upper(), f"{provider}: Expected 'BOW' in response, got: {full_response}"

    logger.info(f"{provider}: Streaming vision inference successful")

