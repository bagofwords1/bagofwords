"""Unit tests for BedrockClient auth modes.

Exercises client construction only — no AWS calls are made. The api_key mode
is verified by emitting a synthetic request-created event and checking that
the Bearer header lands on the request, and that SigV4 signing is disabled.
"""

from __future__ import annotations

import base64
from unittest.mock import MagicMock

import pytest
from botocore import UNSIGNED
from botocore.awsrequest import AWSRequest

from app.ai.llm.clients.bedrock_client import BedrockClient
from app.ai.llm.types import ImageInput, Message

_REGION = "eu-west-1"

# 1x1 transparent PNG, base64-encoded.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def test_unsupported_auth_mode_rejected():
    with pytest.raises(ValueError, match="Unsupported auth_mode"):
        BedrockClient(region=_REGION, auth_mode="oauth")


def test_iam_mode_constructs_client():
    client = BedrockClient(region=_REGION, auth_mode="iam")
    assert client.client.meta.region_name == _REGION


def test_access_keys_mode_requires_both_keys():
    with pytest.raises(ValueError, match="access_keys"):
        BedrockClient(region=_REGION, auth_mode="access_keys", aws_access_key_id="AKIA123")


def test_api_key_mode_requires_key():
    with pytest.raises(ValueError, match="api_key"):
        BedrockClient(region=_REGION, auth_mode="api_key")


def test_api_key_mode_disables_sigv4():
    client = BedrockClient(region=_REGION, auth_mode="api_key", api_key="bedrock-api-key-test")
    assert client.client.meta.config.signature_version is UNSIGNED


def test_api_key_mode_injects_bearer_header():
    api_key = "bedrock-api-key-test"
    client = BedrockClient(region=_REGION, auth_mode="api_key", api_key=api_key)

    request = AWSRequest(method="POST", url="https://bedrock-runtime.eu-west-1.amazonaws.com/")
    client.client.meta.events.emit(
        "request-created.bedrock-runtime.Converse",
        request=request,
        operation_name="Converse",
    )

    assert request.headers["Authorization"] == f"Bearer {api_key}"


def test_api_key_stays_scoped_to_its_client():
    # Two providers in one process must not share credentials — the event
    # handler is registered per client instance, not process-globally.
    client_a = BedrockClient(region=_REGION, auth_mode="api_key", api_key="key-a")
    client_b = BedrockClient(region=_REGION, auth_mode="api_key", api_key="key-b")

    req_a = AWSRequest(method="POST", url="https://bedrock-runtime.eu-west-1.amazonaws.com/")
    req_b = AWSRequest(method="POST", url="https://bedrock-runtime.eu-west-1.amazonaws.com/")
    client_a.client.meta.events.emit(
        "request-created.bedrock-runtime.Converse", request=req_a, operation_name="Converse"
    )
    client_b.client.meta.events.emit(
        "request-created.bedrock-runtime.Converse", request=req_b, operation_name="Converse"
    )

    assert req_a.headers["Authorization"] == "Bearer key-a"
    assert req_b.headers["Authorization"] == "Bearer key-b"


# ---------------------------------------------------------------------------
# Vision: images must actually reach the Converse request in the v2 path.
# Regression for images being silently dropped (model "can't see" the image).
# ---------------------------------------------------------------------------


def _capture_converse_stream(client: BedrockClient) -> dict:
    """Replace converse_stream with a recorder that returns an empty stream and
    returns the dict the captured request kwargs land in after consumption."""
    captured: dict = {}

    def _fake_converse_stream(**kwargs):
        captured.update(kwargs)
        return {"stream": iter([{"messageStop": {"stopReason": "end_turn"}}])}

    client.client = MagicMock()
    client.client.converse_stream.side_effect = _fake_converse_stream
    return captured


async def _drain(agen):
    async for _ in agen:
        pass


@pytest.mark.asyncio
async def test_inference_stream_v2_attaches_images_to_last_user_message():
    client = BedrockClient(region=_REGION, auth_mode="iam")
    captured = _capture_converse_stream(client)

    images = [ImageInput(data=_PNG_B64, media_type="image/png", source_type="base64")]
    messages = [Message(role="user", content="what does this image say?")]

    await _drain(client.inference_stream_v2(model_id="anthropic.claude", messages=messages, images=images))

    sent = captured["messages"]
    last = sent[-1]
    assert last["role"] == "user"
    image_blocks = [b for b in last["content"] if "image" in b]
    assert len(image_blocks) == 1
    img = image_blocks[0]["image"]
    assert img["format"] == "png"
    assert img["source"]["bytes"] == base64.b64decode(_PNG_B64)
    # Image must come BEFORE the text block (Converse ordering requirement).
    assert "image" in last["content"][0]


@pytest.mark.asyncio
async def test_inference_stream_v2_skips_url_images():
    client = BedrockClient(region=_REGION, auth_mode="iam")
    captured = _capture_converse_stream(client)

    images = [ImageInput(data="https://example.com/x.png", source_type="url")]
    messages = [Message(role="user", content="describe this")]

    await _drain(client.inference_stream_v2(model_id="anthropic.claude", messages=messages, images=images))

    last = captured["messages"][-1]
    # URL images are unsupported by Converse — none should be attached, and the
    # text content must remain intact.
    assert all("image" not in b for b in last["content"])
    assert any(b.get("text") == "describe this" for b in last["content"])
