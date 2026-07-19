"""Unit tests for image generation (gpt-image-1) support.

Covers the client generate_image method (both OpenAI clients), the LLM facade
capability gate, the catalog entry, tool registration, the create_artifact
file-embedding schema/prompt wiring, and the <BowFile> sandbox contract doc.
Deterministic — no network, no DB.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.ai.llm.clients.openai_client import OpenAi
from app.ai.llm.clients.openai_responses_client import OpenAIResponsesClient
from app.ai.llm.types import ImageOutput
from app.models.llm_model import LLM_MODEL_DETAILS


def _fake_image_response():
    item = SimpleNamespace(b64_json="QUJDRA==", revised_prompt="a cat, revised")
    usage = SimpleNamespace(input_tokens=11, output_tokens=222)
    return SimpleNamespace(data=[item], usage=usage)


@pytest.mark.parametrize("cls", [OpenAi, OpenAIResponsesClient])
def test_client_generate_image(cls):
    client = cls(api_key="x")
    client.client.images.generate = MagicMock(return_value=_fake_image_response())
    out = asyncio.run(client.generate_image("gpt-image-1", "a cat", size="1024x1024", quality="high"))
    assert isinstance(out, ImageOutput)
    assert out.data == "QUJDRA==" and out.media_type == "image/png"
    assert out.revised_prompt == "a cat, revised"
    assert out.usage.prompt_tokens == 11 and out.usage.completion_tokens == 222
    # size + quality forwarded
    _, kwargs = client.client.images.generate.call_args
    assert kwargs["size"] == "1024x1024" and kwargs["quality"] == "high"


def test_client_generate_image_no_payload_raises():
    client = OpenAi(api_key="x")
    empty = SimpleNamespace(data=[SimpleNamespace(b64_json=None)], usage=None)
    client.client.images.generate = MagicMock(return_value=empty)
    with pytest.raises(RuntimeError):
        asyncio.run(client.generate_image("gpt-image-1", "x"))


def test_facade_gate_rejects_non_image_model():
    """LLM.generate_image must refuse a model without supports_image_generation."""
    from app.ai.llm.llm import LLM

    llm = LLM.__new__(LLM)  # bypass provider/credential setup
    llm.model = SimpleNamespace(supports_image_generation=False)
    llm.model_id = "gpt-5.6-luna"
    llm.provider = "openai"
    with pytest.raises(ValueError):
        llm._validate_image_generation_support()


def test_catalog_has_image_model():
    imgs = [m for m in LLM_MODEL_DETAILS if m.get("supports_image_generation")]
    assert any(m["model_id"] == "gpt-image-1" for m in imgs)
    for m in imgs:
        assert m["provider_type"] == "openai"
        assert not m.get("is_default") and not m.get("is_small_default")


def test_generate_image_tool_registered():
    from app.ai.registry import ToolRegistry
    reg = ToolRegistry()
    meta = reg.get_metadata("generate_image")
    assert meta is not None and meta.category == "action"


def test_create_artifact_accepts_file_ids():
    from app.ai.tools.schemas.create_artifact import CreateArtifactInput
    # file-only artifact: empty visualization_ids is allowed when file_ids given
    data = CreateArtifactInput(prompt="p", visualization_ids=[], file_ids=["abc-123"])
    assert data.file_ids == ["abc-123"]


def test_page_prompt_mentions_bowfile_for_files():
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool
    tool = CreateArtifactTool()
    prompt = tool._build_page_prompt(
        user_prompt="show the image",
        title="Gallery",
        viz_profiles=[],
        instructions_context="",
        report_title="Gallery",
        allow_llm_see_data=True,
        files=[{"id": "file-xyz", "content_type": "image/png", "filename": "a.png"}],
    )
    assert "BowFile" in prompt
    assert "file-xyz" in prompt


def test_sandbox_contract_documents_bowfile():
    from app.ai.tools.implementations._sandbox_context import SANDBOX_RUNTIME_PROMPT
    assert "BowFile" in SANDBOX_RUNTIME_PROMPT
