"""
LLM Client Integration Tests

Tests connectivity and basic inference for LLM provider clients.
Run locally: pytest backend/tests/integrations/llm_clients.py -v
Run specific: pytest backend/tests/integrations/llm_clients.py -k "openai" -v
Run vision tests: pytest backend/tests/integrations/llm_clients.py -k "vision" -v
Run v2 tests: pytest backend/tests/integrations/llm_clients.py -k "v2" -v
"""
import os
import json
import pytest
import logging
from typing import Dict, Any

from app.ai.llm.types import (
    ImageInput,
    Message,
    ToolSpec,
    MessageStopEvent,
    ReasoningDeltaEvent,
    ReasoningStartEvent,
    ReasoningCompleteEvent,
    TextDeltaEvent,
    ToolUseCompleteEvent,
    ToolUseStartEvent,
    UsageEvent,
)
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
    "bedrock",
]

# Test prompt for all providers
TEST_PROMPT = "What is 2 + 2? Reply with just the number."

# Vision-capable providers
VISION_PROVIDERS = [
    "openai",
    "anthropic",
    "google",
    "azure",
    "bedrock",
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
        base_url = kwargs.get("base_url")
        if base_url:
            # Custom base URL → Chat Completions compatible endpoint
            from app.ai.llm.clients.openai_client import OpenAi
            return OpenAi(api_key=kwargs["api_key"], base_url=base_url)
        else:
            # Standard OpenAI → Responses API (supports reasoning)
            from app.ai.llm.clients.openai_responses_client import OpenAIResponsesClient
            return OpenAIResponsesClient(api_key=kwargs["api_key"])
    
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

    elif provider == "bedrock":
        from app.ai.llm.clients.bedrock_client import BedrockClient
        return BedrockClient(
            region=kwargs["region"],
            auth_mode=kwargs.get("auth_mode", "iam"),
            api_key=kwargs.get("api_key"),
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


# =============================================================================
# inference_stream_v2: tool use
# =============================================================================

# A minimal tool the model can call. All providers support this schema shape.
_CALCULATE_TOOL = ToolSpec(
    name="calculate",
    description="Evaluate an arithmetic expression and return the numeric result.",
    input_schema={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "Arithmetic expression to evaluate, e.g. '15 * 17'",
            }
        },
        "required": ["expression"],
    },
)


@pytest.mark.parametrize("provider", LLM_PROVIDERS)
@pytest.mark.asyncio
async def test_llm_inference_stream_v2_tools(provider: str) -> None:
    """
    Test inference_stream_v2 tool-use flow.

    Sends a message that forces tool use and verifies:
    - ToolUseStartEvent + ToolUseCompleteEvent are emitted
    - stop_reason is "tool_use"
    - the tool input contains the expected argument key
    - UsageEvent has non-zero token counts
    """
    cfg = llm_kwargs(provider)
    model_id = cfg.pop("model_id", None)
    if not model_id:
        pytest.skip(f"{provider}: no model_id configured")

    client = get_llm_client(provider, **cfg)
    messages = [Message(role="user", content="Use the calculate tool to compute 15 * 17.")]

    logger.info(f"{provider}: Testing inference_stream_v2 tool use with {model_id}...")

    events = []
    async for evt in client.inference_stream_v2(
        model_id=model_id,
        messages=messages,
        tools=[_CALCULATE_TOOL],
    ):
        events.append(evt)

    tool_starts = [e for e in events if isinstance(e, ToolUseStartEvent)]
    tool_completes = [e for e in events if isinstance(e, ToolUseCompleteEvent)]
    stop_evt = next((e for e in events if isinstance(e, MessageStopEvent)), None)
    usage_evt = next((e for e in events if isinstance(e, UsageEvent)), None)

    assert tool_starts, f"{provider}: No ToolUseStartEvent emitted"
    assert tool_completes, f"{provider}: No ToolUseCompleteEvent emitted"
    assert stop_evt is not None, f"{provider}: No MessageStopEvent emitted"
    assert stop_evt.stop_reason == "tool_use", (
        f"{provider}: Expected stop_reason=tool_use, got {stop_evt.stop_reason}"
    )
    assert usage_evt is not None, f"{provider}: No UsageEvent emitted"
    assert usage_evt.input_tokens > 0, f"{provider}: input_tokens is 0"

    tool = tool_completes[0]
    assert tool.name == "calculate", f"{provider}: Expected tool name 'calculate', got {tool.name!r}"
    assert "expression" in tool.input, (
        f"{provider}: Expected 'expression' in tool input, got {tool.input}"
    )

    logger.info(f"{provider}: tool input={tool.input}, stop_reason={stop_evt.stop_reason}")
    logger.info(f"{provider}: v2 tool use successful")


# =============================================================================
# inference_stream_v2: reasoning / thinking
# Each provider entry may specify an optional "reasoning_model_id" for a model
# that supports extended thinking.  The test is skipped if none is configured.
# =============================================================================

@pytest.mark.parametrize("provider", LLM_PROVIDERS)
@pytest.mark.asyncio
async def test_llm_inference_stream_v2_reasoning(provider: str) -> None:
    """
    Test inference_stream_v2 reasoning / extended-thinking flow.

    Uses "reasoning_model_id" from integrations.json (falls back to "model_id").
    Verifies:
    - ReasoningStartEvent, ReasoningDeltaEvent, ReasoningCompleteEvent are emitted
    - Accumulated reasoning text is non-trivial (> 20 chars)
    - The correct numeric answer (255) appears in the text response
    - UsageEvent has non-zero token counts
    """
    cfg = llm_kwargs(provider)
    model_id = cfg.pop("reasoning_model_id", None) or cfg.pop("model_id", None)
    cfg.pop("model_id", None)  # discard if reasoning_model_id was used
    if not model_id:
        pytest.skip(f"{provider}: no model_id configured")

    client = get_llm_client(provider, **cfg)
    messages = [Message(role="user", content="Think carefully: what is 15 * 17?")]
    thinking_config = {"type": "enabled", "budget_tokens": 5000}

    logger.info(f"{provider}: Testing inference_stream_v2 reasoning with {model_id}...")

    events = []
    async for evt in client.inference_stream_v2(
        model_id=model_id,
        messages=messages,
        thinking=thinking_config,
    ):
        events.append(evt)

    reasoning_starts = [e for e in events if isinstance(e, ReasoningStartEvent)]
    reasoning_deltas = [e for e in events if isinstance(e, ReasoningDeltaEvent)]
    reasoning_completes = [e for e in events if isinstance(e, ReasoningCompleteEvent)]
    text_chunks = [e for e in events if isinstance(e, TextDeltaEvent)]
    usage_evt = next((e for e in events if isinstance(e, UsageEvent)), None)

    reasoning_text = "".join(e.text for e in reasoning_deltas)
    response_text = "".join(e.text for e in text_chunks)

    if not reasoning_starts:
        pytest.skip(f"{provider}: model {model_id} did not emit reasoning events — use a reasoning-capable reasoning_model_id")

    assert reasoning_deltas, f"{provider}: ReasoningStartEvent fired but no ReasoningDeltaEvent"
    assert reasoning_completes, f"{provider}: No ReasoningCompleteEvent emitted"
    assert len(reasoning_text) > 20, (
        f"{provider}: Reasoning text too short ({len(reasoning_text)} chars): {reasoning_text!r}"
    )
    assert "255" in response_text, (
        f"{provider}: Expected '255' in response, got: {response_text!r}"
    )
    assert usage_evt is not None, f"{provider}: No UsageEvent emitted"
    assert usage_evt.input_tokens > 0, f"{provider}: input_tokens is 0"

    logger.info(
        f"{provider}: reasoning_len={len(reasoning_text)}, "
        f"response={response_text[:80]!r}, usage={usage_evt.input_tokens}/{usage_evt.output_tokens}"
    )
    logger.info(f"{provider}: v2 reasoning successful")

